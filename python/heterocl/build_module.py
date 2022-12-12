import io
import os

import hcl_mlir
from hcl_mlir import GlobalInsertionPoint
from hcl_mlir.dialects import hcl as hcl_d
from hcl_mlir.dialects import memref
from hcl_mlir.dialects import func as func_d
from hcl_mlir.execution_engine import *
from hcl_mlir.exceptions import *
from hcl_mlir.ir import *
from hcl_mlir.passmanager import PassManager as mlir_pass_manager

from .devices import Platform
from .context import NestedStageLevel, get_context, get_location, set_context, exit_context
from .module import HCLModule, HCLSuperModule
from .operation import placeholder
from .runtime import copy_build_files
from .schedule import Schedule, Stage
from .utils import get_extra_type_hints
from .passes.pass_manager import PassManager as ast_pass_manager
from .passes.nest_if import NestElseIf
from .passes.promote_func import PromoteFunc
from .ast.ir_builder import IRBuilder


def lower(schedule,
          name="top",
          binds=None,
          simple_mode=False,
          kernel_only=False,
          stmt=None):
    """Lowering step before build into target
       by applying optimization pass
    """
    if schedule.is_lowered():
        raise APIError(
                "The module has been lowered. Please apply schedule primitives before the lowering process."
            )
    # HeteroCL Transformation Pipeline
    ast_pm = ast_pass_manager()
    ast_pm.add_pass(NestElseIf)
    ast_pm.add_pass(PromoteFunc)
    # host_ast, xcel_ast = ast_pm.run(schedule.ast)
    xcel_ast = ast_pm.run(schedule.ast)
    # print(xcel_ast)

    # Build MLIR IR
    set_context()
    xcel_ir_builder = IRBuilder(xcel_ast)
    xcel_ir_builder.build()
    exit_context()

    # set_context()
    # host_ir_builder = IRBuilder(host_ast)
    # host_ir_builder.build()
    # exit_context()

    schedule._device_module = xcel_ir_builder.module
    schedule._device_top = schedule.ast.top_func.ir_op
    # schedule.host_module = host_ir_builder.module

    # MLIR Lowering Pipeline
    hcl_d.loop_transformation(schedule.device_module)
    pipeline = (
        f"func.func"
        f"(affine-loop-normalize, cse, affine-simplify-structures)"
    )
    try:
        with get_context():
            mlir_pass_manager.parse(pipeline).run(schedule.device_module)
    except:
        print(schedule.device_module)
    schedule.set_lowered()
    return schedule.device_module


def build(schedule, target=None, stmt=None, top=None):
    """Build the executable according to the schedule and target.
    """
    try:
        if isinstance(target, Platform) and str(target.tool.mode) != "debug":
            for _, stage in Stage._mapping:
                stage.outline()
        if not schedule.is_lowered():
            lower(schedule)
        if top is not None:
            if not isinstance(top, list):
                top = [top]
            modules = []
            for func in top:
                func_mod = func.build(schedule)
                if target is not None:
                    target.top = func.name
                    original_name = target.project
                    target.project = "{}/{}.prj".format(
                        original_name, func.name)
                    modules.append(build_fpga_kernel(func_mod, target, stmt))
                    target.project = original_name
                else:
                    modules.append(build_llvm(func_mod, target, stmt))
            return HCLSuperModule(modules)
        if target is not None:
            return build_fpga_kernel(schedule, target, stmt)
        else:
            return build_llvm(schedule, target, stmt)
    except Exception as e:
        raise e
    finally:
        hcl_mlir.reset_build_inplace()
        NestedStageLevel.set(0)


def separate_host_device(schedule):
    xcel_module = schedule.create_xcel_module()
    host_module = schedule.create_host_module()

    # create basic components
    hcl_mlir.enable_build_inplace()
    set_context()
    with get_context(), get_location():
        host_tensors = []
        host_nodes = schedule.DataflowGraph.roots + \
            schedule.DataflowGraph.subgraph["outputs"]
        op_map = {}
        # initialization: create host tensors
        for node in host_nodes:
            tensor = node.tensor
            shape = tensor.shape
            loop_names = ["i{}".format(i) for i in range(len(shape))]
            # create new tensors for host
            host_tensor = placeholder(
                shape, name=tensor.op.name+"_host", dtype=tensor.dtype)
            op_map[tensor.op.name] = {"alloc": host_tensor.op}
            if node in schedule.DataflowGraph.subgraph["inputs"] or node in schedule.DataflowGraph.subgraph["outputs"]:
                host_tensors.append(host_tensor.op.result)
            # create initialization loops
            loops = []
            body_ip = GlobalInsertionPoint.get()
            for i, (ub, loop_name) in enumerate(zip(shape, loop_names)):
                loop = hcl_mlir.make_for(
                    0,
                    ub,
                    step=1,
                    name=loop_name,
                    stage=tensor.op.name+"_host" if i == 0 else "",
                    ip=body_ip,
                )
                loops.append(loop)
                body_ip = InsertionPoint(loop.body.operations[0])
            GlobalInsertionPoint.save(body_ip)
            cst = hcl_mlir.ConstantOp(tensor.op.dtype, 0)
            store = hcl_mlir.StoreOp(
                cst, host_tensor.op, [hcl_mlir.IterVar(loop.induction_variable, name=loop_name) for loop, loop_name in zip(loops, loop_names)])
            GlobalInsertionPoint.restore()
        # fix device top function signature
        func_op = schedule.xcel_top
        function_type = FunctionType.get(
            inputs=[node.tensor.memref_type
                    for node in schedule.DataflowGraph.subgraph["inputs"]],
            results=[node.tensor.memref_type for node in schedule.DataflowGraph.subgraph["outputs"]])
        func_op.attributes["function_type"] = TypeAttr.get(function_type)
        func_op.attributes["inputs"] = StringAttr.get(
            ",".join([node.tensor.name+"_xcel" for node in schedule.DataflowGraph.subgraph["inputs"]]))
        itypes = "".join([get_extra_type_hints(
            node.tensor.op.dtype) for node in schedule.DataflowGraph.subgraph["inputs"]])
        func_op.attributes["itypes"] = StringAttr.get(itypes)
        func_op.attributes["outputs"] = StringAttr.get(
            ",".join([node.tensor.name+"_xcel" for node in schedule.DataflowGraph.subgraph["outputs"]]))
        otypes = "".join([get_extra_type_hints(
            node.tensor.op.dtype) for node in schedule.DataflowGraph.subgraph["outputs"]])
        func_op.attributes["itypes"] = StringAttr.get(otypes)
        # preparation: create operation mapping
        for op in schedule.xcel_module.body.operations:
            if "Stage_" in str(op.name):
                name = str(op.name)[1:-1].split("_")[1]  # omit quotation mark
                if name not in op_map:
                    op_map[name] = {"func": op}
        for op in schedule.xcel_top.entry_block.operations:
            if isinstance(op, memref.AllocOp):
                name = str(op.attributes["name"])[1:-1]
                if "alloc" not in op_map[name]:
                    op_map[name]["alloc"] = op
                else:
                    op_map[name]["xcel"] = op
            elif isinstance(op, func_d.CallOp):
                name = str(op.attributes["callee"]).split("_")[1]
                op_map[name]["call"] = op
        for i, param in enumerate(func_op.arguments):
            name = schedule.DataflowGraph.subgraph["inputs"][i].name
            op_map[name]["xcel"] = param
        # traverse the dfg (BFS) and move ops to host based on device_map
        working_set = [node for node in schedule.DataflowGraph.roots]
        flag = False
        while len(working_set) > 0:
            working_node = working_set.pop(0)
            name = working_node.name
            if working_node not in host_nodes and schedule.DataflowGraph.device_map[name] == "CPU":
                op_map[name]["func"].move_before(schedule._host_top)
                if "alloc" in op_map[name]:
                    op_map[name]["alloc"].move_before(schedule._host_ret)
                op_map[name]["call"].move_before(schedule._host_ret)
                # update reference
                for i, parent in enumerate(working_node.parents):
                    if "alloc" in op_map[parent.name]:
                        op_map[name]["call"].operands[i] = op_map[parent.name]["alloc"].result
            elif schedule.DataflowGraph.device_map[name] == "FPGA":
                if not flag:
                    flag = True
                    # call device function
                    for node in schedule.DataflowGraph.subgraph["inputs"]:
                        if node not in schedule.DataflowGraph.roots:
                            host_tensors.insert(
                                0, op_map[node.name]["alloc"].result)
                    call_op = hcl_mlir.CallOp(None, "top", host_tensors)
                    call_op.built_op.attributes["inputs"] = StringAttr.get(
                        ",".join([node.tensor.name for node in schedule.DataflowGraph.subgraph["inputs"]]))
                    call_op.built_op.attributes["outputs"] = StringAttr.get(
                        ",".join([node.tensor.name for node in schedule.DataflowGraph.subgraph["outputs"]]))
                # update reference
                for i, parent in enumerate(working_node.parents):
                    if parent.base is not None:
                        op_dict = op_map[parent.base.name]
                    else:
                        op_dict = op_map[parent.name]
                    if "xcel" in op_dict:
                        if isinstance(op_dict["xcel"], hcl_mlir.BlockArgument):
                            op_map[name]["call"].operands[i] = op_dict["xcel"]
                        else:
                            op_map[name]["call"].operands[i] = op_dict["xcel"].result
                    else:
                        op_map[name]["call"].operands[i] = op_dict["alloc"].result
                if working_node in schedule.DataflowGraph.subgraph["outputs"]:
                    if working_node.base is not None:
                        op_dict = op_map[working_node.base.name]
                    else:
                        op_dict = op_map[working_node.name]
                    if "xcel" in op_dict:
                        if isinstance(op_dict["xcel"], hcl_mlir.BlockArgument):
                            schedule._xcel_ret.operands[0] = op_dict["xcel"]
                        else:
                            schedule._xcel_ret.operands[0] = op_dict["xcel"].result
                    else:
                        schedule._xcel_ret.operands[0] = op_dict["alloc"].result

            for child in working_node.children:
                working_set.append(child)

    hcl_mlir.disable_build_inplace()


def generate_kernel_header(schedule):
    header = """#ifndef KERNEL_H
#define KERNEL_H

#include <ap_int.h>
#include <ap_fixed.h>
#include <hls_stream.h>

void top("""
    all_inputs_outputs = schedule.DataflowGraph.subgraph["inputs"] + \
        schedule.DataflowGraph.subgraph["outputs"]
    args = []
    for node in all_inputs_outputs:
        tensor = node.tensor.op
        with get_context():
            arg = hcl_mlir.print_mlir_type(
                hcl_mlir.get_mlir_type(tensor.dtype)) + " " + tensor.name
        for index in tensor.shape:
            arg += "[{}]".format(index)
        args.append(arg)
    header += ", ".join(args)
    header += ");\n\n#endif // KERNEL_H"
    return header


def build_fpga_kernel(schedule, target=None, stmt=None):
    if isinstance(schedule, Schedule):
        device_module = schedule.device_module
    else:
        device_module = schedule
    if target == "vhls":
        buf = io.StringIO()
        hcl_d.emit_vhls(device_module, buf)
        buf.seek(0)
        hls_code = buf.read()
        return hls_code
    elif target == "ihls":
        buf = io.StringIO()
        hcl_d.emit_ihls(device_module, buf)
        buf.seek(0)
        hls_code = buf.read()
        return hls_code
    elif not isinstance(target, Platform):
        raise RuntimeError("Not supported target")

    if str(target.tool.mode) == "debug":
        # make the project folder and copy files
        copy_build_files(target)

        buf = io.StringIO()
        hcl_d.emit_vhls(device_module, buf)
        buf.seek(0)
        hls_code = buf.read()
        with open("{}/kernel.cpp".format(target.project), "w") as outfile:
            outfile.write(hls_code)
        host_code = None
        with open("{}/host.cpp".format(target.project), "w") as outfile:
            outfile.write("")

    else:
        # make the project folder and copy files
        copy_build_files(target)

        # data placement
        schedule.DataflowGraph.graph_partition()
        separate_host_device(schedule)

        # generate xcel code
        buf = io.StringIO()
        hcl_d.emit_vhls(schedule.xcel_module, buf)
        buf.seek(0)
        hls_code = buf.read()
        with open("{}/kernel.cpp".format(target.project), "w") as outfile:
            outfile.write(hls_code)

        # generate host code
        host_buf = io.StringIO()
        hcl_d.emit_vhls(schedule.host_module, host_buf)
        host_buf.seek(0)
        host_code = host_buf.read()
        with open("{}/host.cpp".format(target.project), "w") as outfile:
            outfile.write(host_code)

        # generate header
        header = generate_kernel_header(schedule)
        with open("{}/kernel.h".format(target.project), "w") as outfile:
            outfile.write(header)

    hcl_module = HCLModule(target.top, hls_code, target, host_src=host_code)
    return hcl_module


def build_llvm(schedule, target=None, stmt=None):
    name = 'top'
    with get_context() as ctx, get_location():
        if isinstance(schedule, Schedule):
            func = schedule.device_top
            func.attributes['llvm.emit_c_interface'] = UnitAttr.get()
            func.attributes[name] = UnitAttr.get()
            module = Module.parse(str(schedule.device_module), ctx)
        else:
            module = Module.parse(str(schedule), ctx)
            for op in module.body.operations:
                if isinstance(op, func_d.FuncOp):
                    func = op
                    break
                else:
                    raise APIError("No top-level function found in the built MLIR module")
            func.attributes['llvm.emit_c_interface'] = UnitAttr.get()
            func.attributes[name] = UnitAttr.get()
            func.attributes['sym_name'] = StringAttr.get("top")
        host_src = Module.parse(str(module))
        # memref dce should precede lower_composite_type
        hcl_d.memref_dce(module) 
        hcl_d.lower_composite_type(module)
        hcl_d.lower_fixed_to_int(module)
        hcl_d.lower_print_ops(module)
        hcl_d.lower_anywidth_int(module)
        # Note: lower_any_width_int should precede
        # move_return_to_input, because it uses input/output
        # type hints.
        hcl_d.move_return_to_input(module)
        hcl_d.lower_bit_ops(module)
        hcl_d.legalize_cast(module)
        hcl_d.remove_stride_map(module)
        hcl_d.lower_hcl_to_llvm(module, ctx)
        # num_results = len(func.type.results)
        num_results = 0
        
        # Add shared library
        if os.getenv("LLVM_BUILD_DIR") is not None:
            shared_libs = [
                os.path.join(os.getenv("LLVM_BUILD_DIR"),
                            'lib', 'libmlir_runner_utils.so'),
                os.path.join(os.getenv("LLVM_BUILD_DIR"),
                            'lib', 'libmlir_c_runner_utils.so')
            ]
        else:
            APIWarning("LLVM_BUILD_DIR is not set, print memref feature is not available.").warn()
            shared_libs = None

        if shared_libs is not None:
            execution_engine = ExecutionEngine(module, opt_level=0, shared_libs=shared_libs)
        else:
            execution_engine = ExecutionEngine(module, opt_level=0)
        hcl_module = HCLModule(name, execution_engine,
                               "llvm", host_src=host_src, return_num=num_results)
        return hcl_module
