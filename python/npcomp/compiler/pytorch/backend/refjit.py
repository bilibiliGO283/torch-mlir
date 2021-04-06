#  Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
#  See https://llvm.org/LICENSE.txt for license information.
#  SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception

import os

import torch

from mlir.ir import *
from mlir.passmanager import *
from npcomp.compiler.generic.backend import refjit as refjit_backend
from npcomp.compiler.utils import logging

__all__ = [
    "is_enabled",
    "CompilerBackend",
]

# The set of passes that lowers from a TorchScript object graph representation
# to a module semantics where symbols correspond to dotted paths into the
# module.
OBJECT_GRAPH_LOWERING_PASSES = (
    # Globalize the program. The rest of the compiler assumes a globalized
    # program, which makes all analyses and transforms significantly easier
    # to write.
    "torch-globalize-pipeline",
    # symbol-dce is currently needed for correctness, as we don't have a lowering
    # in the backend for torch.global_slot's.
    # Torch usually inserts a few unused global slots that are otherwise
    # bothersome because we don't currently have a lowering for them.
    # TODO: Support global slots in backends.
    "symbol-dce",
    # Incorporate user annotations and remove signature Python-isms.
    "torch-adjust-calling-conventions",
)

# TODO: Replace this with lowering to "TCP + guards" -- that's the real
# backend interface. Put differently, add "TCF to TCP" to the end of this
# pipeline.
TORCH_TO_TCF_PASSES = (
    # Recognize ATen kernels.
    "func(aten-recognize-kernels)",

    # Convert the bulk of the program to ranked tensors with known dtype.
    # This is the input to the backend layer that we are aiming for.

    # First, unilaterally convert public functions to tensor.
    # The way this pass is currently written, this implies that
    # as pipeline authors, we are restricting our users to not be able to see
    # updates to "out params" on their public functions.
    # This is deemed ok for now.
    "numpy-public-functions-to-tensor",
    # Convert the bulk of non-ABI-visible arrays to tensors.
    "func(numpy-array-to-tensor)",
    # Do shape and dtype refinement.
    # We could do it sooner, but the pass currently doesn't have transfer
    # functions for array ops.
    "func(torch-refine-types)",
    # Propagate to ABI return types the shape/dtype information discovered by
    # the previous pass. Doing this is ABI-compatible for our backends.
    "numpy-refine-public-return",
    # Clean up a few stray array/tensor conversion remnants.
    "func(numpy-array-to-tensor)",

    # Lower to TCF which is the input to RefBackend.
    # Most of this pass should be subsumed by aten->linalg+guards conversions.
    # (the guard generation will be automated from the linalg Op DSL)
    "func(convert-aten-to-tcf)",
)

# Re-export.
is_enabled = refjit_backend.is_enabled


class TorchJitModuleInvoker(refjit_backend.JitModuleInvoker):
  """Allows torch.Tensor inputs to be passed to module invocations."""

  def __getitem__(self, function_name: str):
    numpy_invoke = super().__getitem__(function_name)

    def invoke(*args):
      args = tuple(
          arg.numpy() if isinstance(arg, torch.Tensor) else arg for arg in args)
      return numpy_invoke(*args)

    return invoke


class CompilerBackend:
  """Main entry-point for the backend."""

  def __init__(self):
    super().__init__()
    self._refjit = refjit_backend.get_refjit()
    self._debug = logging.debug_enabled()

  def compile(self, imported_module: Module):
    """Compiles an imported module, with a flat list of functions.

    Args:
      imported_module: The MLIR module consisting of funcs in the torch
        dialect.
    Returns:
      An opaque, backend specific module object that can be passed to load.
      The object may actually be something more specific to the backend (i.e.
      for IREE, it is a serialized VM flatbuffer) but the contract is that
      it is operated on by methods on this class.
    """
    with imported_module.context as context:
      if self._debug:
        logging.debug("Initial PyTorch IR:\n{}", imported_module)

      # Frontend.
      pipeline_str = ",".join(TORCH_TO_TCF_PASSES)
      if self._debug:
        logging.debug("Running Torch->TCF pipeline '{}'", pipeline_str)
      pm = PassManager.parse(pipeline_str)
      pm.run(imported_module)
      if self._debug:
        logging.debug("TCF IR:\n{}", imported_module)

      # Backend.
      # Note that this is a separate pass manager purely to aid in debugging.
      pm = PassManager()
      self._refjit.build_backend_compilation_pipeline(pm)
      pm.run(imported_module)
      if self._debug:
        logging.debug("Backend IR:\n{}", imported_module)

    jit_module = self._refjit.JITModule.from_compiled_module(
        imported_module, refjit_backend.get_runtime_libs())
    return jit_module

  def compile_object_graph(self, imported_module: Module):
    """Compiles an imported module, with TorchScript object graph semantics.

    Args:
      imported_module: The MLIR module consisting of IR as imported by the
      torch_mlir.import_module
    Returns:
      An opaque, backend specific module object that can be passed to load.
      The object may actually be something more specific to the backend (i.e.
      for IREE, it is a serialized VM flatbuffer) but the contract is that
      it is operated on by methods on this class.
    """
    with imported_module.context as context:
      if self._debug:
        logging.debug("Initial PyTorch object graph IR:\n{}", imported_module)

      # Frontend.
      pipeline_str = ",".join(OBJECT_GRAPH_LOWERING_PASSES)
      if self._debug:
        logging.debug(
            "Running Torch object graph lowering pipeline '{}'", pipeline_str)
      pm = PassManager.parse(pipeline_str)
      pm.run(imported_module)
    return self.compile(imported_module)

  def load(self, jit_module) -> TorchJitModuleInvoker:
    """Loads a compiled artifact into the runtime."""
    return TorchJitModuleInvoker(jit_module)
