"""The computation schedule api of TVM."""
from __future__ import absolute_import as _abs
from ._ffi.base import string_types
from ._ffi.node import NodeBase, register_node
from ._ffi.function import _init_api
from ..devices import Device, DevMemoryPair
from . import _api_internal
from . import tensor as _tensor
from . import expr as _expr
from . import stmt as _stmt
from . import container as _container
from ..mlir.base import get_module
from hcl_mlir import get_insertion_point, get_context, get_location
from mlir.ir import *
import hcl_mlir
from mlir.dialects import builtin, std

@register_node
class Buffer(NodeBase):
    """Symbolic data buffer in TVM.

    Buffer provide a way to represent data layout
    specialization of data structure in TVM.

    Do not construct directly, use :any:`decl_buffer` instead.
    See the documentation of :any:`decl_buffer` for more details.

    See Also
    --------
    decl_buffer : Declare a buffer
    """
    READ = 1
    WRITE = 2

    def access_ptr(self, access_mask, ptr_type="handle", content_lanes=1, offset=0):
        """Get an access pointer to the head of buffer.

        This is the recommended method to get buffer data
        ptress when interacting with external functions.

        Parameters
        ----------
        access_mask : int
            The access pattern MASK. Indicate whether the
            access will read or write to the data content.

        ptr_type : str, optional
            The data type of the result pointer. Do not specify
            unless we want to cast pointer to specific type.

        content_lanes: int, optional
            The number of lanes for the data type. This value
            is greater than one for vector types.

        offset: int, optional
            The offset of pointer. We can use it to offset by
            the number of elements from the address of ptr.

        Examples
        --------
        .. code-block:: python

          import tvm.schedule.Buffer
          # Get access ptr for read
          buffer.access_ptr("r")
          # Get access ptr for read/write with bitmask
          buffer.access_ptr(Buffer.READ | Buffer.WRITE)
          # Get access ptr for read/write with str flag
          buffer.access_ptr("rw")
        """
        if isinstance(access_mask, string_types):
            mask = 0
            for value in access_mask:
                if value == "r":
                    mask = mask | Buffer.READ
                elif value == "w":
                    mask = mask | Buffer.WRITE
                else:
                    raise ValueError("Unknown access_mask %s" % access_mask)
            access_mask = mask
        return _api_internal._BufferAccessPtr(self, access_mask, ptr_type,
                                              content_lanes, offset)

    def vload(self, begin, dtype=None):
        """Generate an Expr that loads dtype from begin index.

        Parameters
        ----------
        begin : Array of Expr
            The beginning index in unit of Buffer.dtype

        dtype : str
            The data type to be loaded,
            can be vector type which have lanes that is multiple of Buffer.dtype

        Returns
        -------
        load : Expr
            The corresponding load expression.
        """
        begin = (begin,) if isinstance(begin, (int, _expr.Expr)) else begin
        dtype = dtype if dtype else self.dtype
        return _api_internal._BufferVLoad(self, begin, dtype)

    def vstore(self, begin, value):
        """Generate a Stmt that store value into begin index.

        Parameters
        ----------
        begin : Array of Expr
            The beginning index in unit of Buffer.dtype

        value : Expr
            The value to be stored.

        Returns
        -------
        store : Stmt
            The corresponding store stmt.
        """
        begin = (begin,) if isinstance(begin, (int, _expr.Expr)) else begin
        return _api_internal._BufferVStore(self, begin, value)


@register_node
class Split(NodeBase):
    """Split operation on axis."""
    pass


@register_node
class Fuse(NodeBase):
    """Fuse operation on axis."""
    pass


@register_node
class IterVar(NodeBase, _expr.ExprOp):
    """Represent iteration variable.

    IterVar is normally created by Operation, to represent
    axis iterations in the computation.
    It can also created by schedule primitives like :any:`tvm.schedule.Stage.split`.

    See Also
    --------
    tvm.thread_axis: Create thread axis IterVar.
    tvm.reduce_axis: Create reduce axis IterVar.
    """
    DataPar = 0
    ThreadIndex = 1
    CommReduce = 2
    Ordered = 3
    DimInfo = 4
    Unrolled = 5
    Vectorized = 6
    Parallelized = 7
    Tensorized = 8
    Pipelined = 9

_tensor.iter_var_cls = IterVar

def create_schedule(ops):
    """Create a schedule for list of ops

    Parameters
    ----------
    ops : list of Operations
        The source expression.

    Returns
    -------
    sch : schedule.Schedule
        The created schedule.
    """
    if not isinstance(ops, (list, _container.Array)):
        ops = [ops]
    return _api_internal._CreateSchedule(ops)


@register_node("Schedule")
class _Schedule(NodeBase):
    """Schedule for all the stages."""
    def __getitem__(self, k):
        if isinstance(k, _tensor._Tensor):
            k = k.op
        if not isinstance(k, _tensor.Operation):
            raise ValueError("Expect schedule key to be Tensor or Operation")
        if k not in self.stage_map:
            raise ValueError("Cannot find the operation %s in schedule" % (str(k)))
        return self.stage_map[k]

    def normalize(self):
        """Build a normalized schedule from the current schedule.

        Insert necessary rebase to make certain iter var to start from 0.
        This is needed before bound inference and followup step.

        Returns
        -------
        sch : Schedule
            The normalized schedule.
        """
        return _api_internal._ScheduleNormalize(self)

    def create_group(self, outputs, inputs, include_inputs=False):
        """Create stage group by giving output and input boundary.

        The operators between outputs and inputs are placed as member of group.
        outputs are include in the group, while inputs are not included.

        Parameters
        ----------
        outputs : list of Tensors
            The outputs of the group.

        inputs : list of Tensors
            The inputs of the group.

        include_inputs : boolean, optional
            Whether include input operations in the group if they are used by outputs.

        Returns
        -------
        group : Stage
            A virtual stage represents the group, user can use compute_at to move
            the attachment point of the group.
        """
        if isinstance(outputs, _tensor._Tensor):
            outputs = [outputs]
        if isinstance(inputs, _tensor._Tensor):
            inputs = [inputs]
        return _api_internal._ScheduleCreateGroup(
            self, outputs, inputs, include_inputs)

    def reuse_at(self, target, parent, axis, name):
        """Create a reuse buffer reusing the output of current stage

        This returns a new tensor representing the reuse buffer. A stage
        is also built correspondingly. The new stage will be a sub-stage of
        the parent stage under the specified axis. Thus, the axis must be
        inside the axis list of the parent stage.

        Parameters
        ----------
        target : Tensor
            The tensor whose values will be reused
        parent : Stage
            The stage that reuses the output of the current stage
        axis : IterVar
            The axis that generates the resue values
        name : string
            The name of the reuse buffer

        Returns
        -------
        Tensor
        """
        with get_context() as ctx, get_location() as loc:
            i32 = IntegerType.get_signless(32)
            f32 = F32Type.get(ctx)
            # TODO: Need to do shape inference
            memref_type = MemRefType.get(target.shape, f32, loc=loc)
            res = hcl_mlir.ReuseAtOp(memref_type, parent.stage_handle.result, target.op.result, axis.result, ip=get_insertion_point())
        # return _api_internal._ScheduleReuseAt(self, target, parent, axis, name)

    def buffer_at(self, target, parent, axis, name):
        """Create a write buffer reusing the output of current stage

        This returns a new tensor representing the write buffer.
        A stage is also built correspondingly.
        The new stage will be a sub-stage of the parent stage
        under the specified axis.
        Thus, the axis must be inside the axis list of
        the parent stage.

        Parameters
        ----------
        target : Tensor
            The tensor whose values will be buffered
        parent : Stage
            The stage that includes the target tensor
        axis : IterVar
            The axis to be buffered
        name : string
            The name of the write buffer

        Returns
        -------
        Tensor
        """
        with get_context() as ctx, get_location() as loc:
            i32 = IntegerType.get_signless(32)
            f32 = F32Type.get(ctx)
            # TODO: Need to do shape inference
            memref_type = MemRefType.get(target.shape, f32, loc=loc)
            res = hcl_mlir.BufferAtOp(memref_type, parent.stage_handle.result, target.op.result, axis.result, ip=get_insertion_point())

    def partition(self, target, partition_type, dim, factor):
        with get_context() as ctx, get_location():
            i32 = IntegerType.get_signless(32)
            # TODO: Change to enum type
            if partition_type == _stmt.Partition.Complete:
                partition_type = IntegerAttr.get(i32, 0)
            elif partition_type == _stmt.Partition.Block:
                partition_type = IntegerAttr.get(i32, 1)
            elif partition_type == _stmt.Partition.Cyclic:
                partition_type = IntegerAttr.get(i32, 2)
            else:
                raise RuntimeError("Not supported partition type")
            factor = IntegerAttr.get(i32, factor)
            dim = IntegerAttr.get(i32, dim)
            res = hcl_mlir.PartitionOp(target.op.result, partition_type, dim, factor, ip=get_insertion_point())
        # return _api_internal._SchedulePartition(self, target, dim, factor, partition_type)

    # Create separate python functions for data movement FFIs
    # Move a stage's loop body to device
    def in_stage_move(self, target, dst, src, axis=0, 
           io_type=_expr.IO.DMA, depth=1):
        dst = 1 if 'fpga' in str(dst) else 0
        return  _api_internal._ScheduleInStageMove(
                   self, target, dst, io_type, depth, axis)
    
    # Move a placeholder or extern op to device
    def move_to_device(self, target, dst, src, dev_port, axis=0, 
           io_type=_expr.IO.DMA, depth=1):
        dst = 1 if 'fpga' in str(dst) else 0
        return _api_internal._ScheduleMove(self, target, src, dst,
                io_type, depth, dev_port)

    # Stream between two HCL modules
    def inter_module_stream(self, target, dst_stage, src_stage, 
           match, axis=0, io_type=_expr.IO.DMA, depth=1):
        return _api_internal._ScheduleStream(self, target, 
                dst_stage, src_stage, match, io_type, depth, axis)

    # Stream from local buffer to HCL module
    def local_buffer_to_module_stream(self, target, dst, src, 
           match, axis=0, io_type=_expr.IO.DMA, depth=1):
        return _api_internal._ScheduleMoveToStage(self, target, 
                dst, match, io_type, depth, "stream")

    # Stream FIFO between HLC stages
    def inter_stage_stream(self, target, dst, src, 
           axis=0, io_type=_expr.IO.DMA, depth=1):
        index_lst = []
        return _api_internal._ScheduleStream(self, target, dst, src, 
                index_lst, io_type, depth, axis)

    # Link explicitly unrolled PEs
    def create_inter_pe_channel(self, target, dst, src, depth=1):
        return _api_internal._SchedulePeLinking(self, target, 
            dst, src, depth)       

@register_node("Stage")
class _Stage(NodeBase):
    """A Stage represents schedule for one operation.

    These scheduling functions can be accessed by the Stage generated by HeteroCL APIs
    """
    def split(self, parent, factor=None, nparts=None, mode="transform"):
        """Split the stage either by factor providing outer scope, or both

        Parameters
        ----------
        parent : IterVar
            The parent iter var.

        factor : Expr, optional
            The splitting factor

        nparts : Expr, optional
            The number of outer parts.

        mode : str, "transform" or "annotate"
            "transform" mode changes the IR structure,
            "annotate" mode adds attributes.

        Returns
        -------
        outer : IterVar
            The outer variable of iteration.

        inner : IterVar
            The inner variable of iteration.
        """
        if isinstance(parent, int):
            parent = self.op.axis[parent]
        var = parent
        with get_context() as ctx, get_location():
            i32 = IntegerType.get_signless(32)
            factor = IntegerAttr.get(i32, factor)
            loop_handle_type = hcl_mlir.LoopHandleType.get(ctx)
            split_op = hcl_mlir.SplitOp(loop_handle_type, loop_handle_type, self.stage_handle.result, var.result, factor, ip=get_insertion_point())
        return split_op.results[0], split_op.results[1]
        # if nparts is not None:
        #     if factor is not None:
        #         raise ValueError("Donot need to provide both outer and nparts")
        #     if mode == "annotate":
        #         _api_internal._StageSplitByNPartsAnnotate(self, parent, nparts)
        #     elif mode == "transform":
        #         outer, inner = _api_internal._StageSplitByNParts(self, parent, nparts)
        #         return outer, inner
        #     else:
        #         raise ValueError("split mode must be transform or annotate")
        # else:
        #     if factor is None:
        #         raise ValueError("Either nparts or factor need to be provided")
        #     if mode == "annotate":
        #         _api_internal._StageSplitByFactorAnnotate(self, parent, factor)
        #     elif mode == "transform":
        #         outer, inner = _api_internal._StageSplitByFactor(self, parent, factor)
        #         return outer, inner
        #     else:
        #         raise ValueError("split mode must be transform or annotate")

    def fuse(self, *args):
        """Fuse multiple consecutive iteration variables into a single iteration variable.

        fused = fuse(...fuse(fuse(args[0], args[1]), args[2]),..., args[-1])
        The order is from outer to inner.

        Parameters
        ----------
        args : list of IterVars
            Itervars that proceeds each other

        Returns
        -------
        fused : IterVar
            The fused variable of iteration.
        """
        assert len(args) >= 1, "Length of the arguments must be >=1 for fuse."
        args = list(args)
        for i in range(0, len(args)):
            if isinstance(args[i], int):
                args[i] = self.op.axis[args[i]]
            if not isinstance(args[i], OpResult):
                args[i] = args[i].result
        with get_context() as ctx, get_location():
            loop_handle_type = hcl_mlir.LoopHandleType.get(ctx)
            fused = hcl_mlir.FuseOp(loop_handle_type, self.stage_handle.result, args, ip=get_insertion_point())
        return fused

    def set_scope(self, scope):
        """Set the thread scope of this stage

        Parameters
        ----------
        scope : str
            The thread scope of this stage
        """
        return _api_internal._StageSetScope(self, scope)

    def bind(self, ivar, thread_ivar):
        """Bind ivar to thread index thread_ivar

        Parameters
        ----------
        ivar : IterVar
            The iteration to be binded to thread.

        thread_ivar : IterVar
            The thread to be binded.
        """
        _api_internal._StageBind(self, ivar, thread_ivar)

    def env_threads(self, threads):
        """Mark threads to be launched at the outer scope of composed op.

        Parameters
        ----------
        threads : list of threads
            The threads to be launched.
        """
        if isinstance(threads, IterVar):
            threads = [threads]
        _api_internal._StageEnvThreads(self, threads)

    def set_store_predicate(self, predicate):
        """Set predicate under which store to the array can be performed.

        Use this when there are duplicated threads doing the same store and we only
        need one of them to do the store.

        Parameters
        ----------
        predicate : Expr
            The guard condition fo store.
        """
        _api_internal._StageSetStorePredicate(self, predicate)

    def compute_at(self, parent, scope):
        """Attach the stage at parent's scope

        Parameters
        ----------
        parent : _Stage
            The parent stage

        scope : IterVar
            The loop scope t be attached to.
        """
        if isinstance(scope, int):
            scope = parent.op.axis[scope]
        with get_context() as ctx, get_location():
            loop_handle_type = hcl_mlir.LoopHandleType.get(ctx)
            fused = hcl_mlir.ComputeAtOp(self.stage_handle.result, parent.stage_handle.result, scope.result, ip=get_insertion_point())
        # _api_internal._StageComputeAt(self, parent, scope)

    def compute_inline(self):
        """Mark stage as inline

        Parameters
        ----------
        parent : Stage
            The parent stage
        """
        _api_internal._StageComputeInline(self)

    def compute_root(self):
        """Attach the stage at parent, and mark it as root

        Parameters
        ----------
        parent : Stage
            The parent stage
        """
        _api_internal._StageComputeRoot(self)

    def reorder(self, *args):
        """reorder the arguments in the specified order.

        Parameters
        ----------
        args : list of IterVar
            The order to be ordered
        """
        args = list(args)
        for i in range(0, len(args)):
            if isinstance(args[i], int):
                args[i] = self.op.axis[args[i]]
            if not isinstance(args[i], OpResult):
                args[i] = args[i].result
        with get_context(), get_location():
            hcl_mlir.ReorderOp(self.stage_handle.result, args, ip=get_insertion_point())
        # _api_internal._StageReorder(self, args)

    def tile(self, x_parent, y_parent, x_factor, y_factor):
        """ Perform tiling on two dimensions

        The final loop order from outmost to inner most are
        [x_outer, y_outer, x_inner, y_inner]

        Parameters
        ----------
        x_parent : IterVar
            The original x dimension
        y_parent : IterVar
            The original y dimension
        x_factor : Expr
            The stride factor on x axis
        y_factor : Expr
            The stride factor on y axis

        Returns
        -------
        x_outer : IterVar
            Outer axis of x dimension
        y_outer : IterVar
            Outer axis of y dimension
        x_inner : IterVar
            Inner axis of x dimension
        p_y_inner : IterVar
            Inner axis of y dimension
        """
        with get_context() as ctx, get_location():
            i32 = IntegerType.get_signless(32)
            x_factor = IntegerAttr.get(i32, x_factor)
            y_factor = IntegerAttr.get(i32, y_factor)
            loop_handle_type = hcl_mlir.LoopHandleType.get(ctx)
            tile_op = hcl_mlir.TileOp(loop_handle_type, loop_handle_type, loop_handle_type, loop_handle_type, self.stage_handle.result, x_parent.result, y_parent.result, x_factor, y_factor, ip=get_insertion_point())
        # x_outer, y_outer, x_inner, y_inner = _api_internal._StageTile(
        #     self, x_parent, y_parent, x_factor, y_factor)
        # return x_outer, y_outer, x_inner, y_inner
        return tile_op.results[0], tile_op.results[1], \
            tile_op.results[2], tile_op.results[3]

    def vectorize(self, var):
        """Vectorize the iteration.

        Parameters
        ----------
        var : IterVar
            The iteration to be vectorize
        """
        _api_internal._StageVectorize(self, var)

    def unroll(self, var, factor=0):
        """Unroll the iteration.

        Parameters
        ----------
        var : IterVar
            The iteration to be unrolled.

        factor : Expr
            The unroll factor.
            Default value 0 means full unroll.
        """
        if isinstance(var, int):
            var = self.op.axis[var]
        with get_context(), get_location():
            i32 = IntegerType.get_signless(32)
            factor = IntegerAttr.get(i32, factor)
            hcl_mlir.UnrollOp(self.stage_handle.result, var.result, factor, ip=get_insertion_point())
        # _api_internal._StageUnroll(self, var, factor)

    def parallel(self, var):
        """Parallelize the iteration.

        Parameters
        ----------
        var : IterVar
            The iteration to be parallelized.
        """
        if isinstance(var, int):
            var = self.op.axis[var]
        with get_context(), get_location():
            hcl_mlir.ParallelOp(self.stage_handle.result, var.result, ip=get_insertion_point())
        # _api_internal._StageParallel(self, var)
    
    def dataflow(self, var=None):
        """Create dataflow region inside loop or function body

        Parameters
        ----------
        var : IterVar
            The iteration of the target loop
        """
        if isinstance(var, int):
            var = self.op.axis[var]
        _api_internal._StageDataflow(self, var)

    def pipeline(self, var, initiation_interval=1):
        """Pipeline the iteration.

        Parameters
        ----------
        var : IterVar
            The iteration to be pipelined.

        initiation_interval : Expr
            The initiation interval in pipeline schedule.
            Default value is 1.
        """
        if isinstance(var, int):
            var = self.op.axis[var]
        with get_context(), get_location():
            i32 = IntegerType.get_signless(32)
            ii = IntegerAttr.get(i32, initiation_interval)
            hcl_mlir.PipelineOp(self.stage_handle.result, var.result, ii, ip=get_insertion_point())
        # _api_internal._StagePipeline(self, var, initiation_interval)

    def stencil(self, burst_width=512, unroll_factor=1, num_iteration=1):
        _api_internal._StageStencil(self, burst_width, unroll_factor, num_iteration)

    def pragma(self, var, pragma_type):
        """Annotate the iteration with pragma

        This will translate to a pragma_scope surrounding
        the corresponding loop generated.
        Useful to support experimental features and extensions.

        Parameters
        ----------
        var : IterVar
            The iteration to be anotated

        pragma_type : str
             The pragma string to be annotated

        Note
        ----
        Most pragmas are advanced/experimental features
        and may subject to change. List of supported pragmas:

        - **debug_skip_region**

          Force skip the region marked by the axis and turn it into no-op.
          This is useful for debug purposes.

        - **parallel_launch_point**

          Specify to launch parallel threads outside the
          specified iteration loop. By default the threads
          launch at the point of parallel construct.
          This pragma moves the launching point to even outer scope.
          The threads are launched once and reused across multiple
          parallel constructs as BSP style program.

        - **parallel_barrier_when_finish**

          Insert a synchronization barrier between working threads
          after the specified loop iteration finishes.

        - **parallel_stride_pattern**

          Hint parallel loop to execute in strided pattern.
          :code:`for (int i = task_id; i < end; i += num_task)`

        """
        _api_internal._StagePragma(self, var, pragma_type)

    def prefetch(self, tensor, var, offset):
        """Prefetch the specified variable

        Parameters
        ----------
        tensor : Tensor
            The tensor to be prefetched
        var : IterVar
            The loop point at which the prefetching is applied
        offset : Expr
            The number of iterations to be prefetched before actual execution
        """
        _api_internal._StagePrefetch(self, tensor, var, offset)

    def storage_align(self, axis, factor, offset):
        """Set alignment requirement for specific axis

        This ensures that stride[axis] == k * factor + offset for some k.
        This is useful to set memory layout to for more friendly memory
        access pattern. For example, we can set alignment to be
        factor=2, offset=1 to avoid bank conflict for thread access on
        higher dimension in GPU shared memory.

        Parameters
        ----------
        axis : IterVar
            The axis dimension to be aligned.
        factor : int
            The factor in alignment specification.
        offset : int
            The offset in the alignment specification.
        """
        _api_internal._StageStorageAlign(self, axis, factor, offset)

    def double_buffer(self):
        """Compute the current stage via double buffering.

        This can only be applied to intermediate stage.
        This will double the storage cost of the current stage.
        Can be useful to hide load latency.
        """
        _api_internal._StageDoubleBuffer(self)

    def opengl(self):
        """The special OpenGL schedule

        Maps each output element to a pixel.
        """
        _api_internal._StageOpenGL(self)

_init_api("tvm.schedule")
