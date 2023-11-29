# Copyright (C) 2020-2023 Sebastian Blauth
#
# This file is part of cashocs.
#
# cashocs is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# cashocs is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with cashocs.  If not, see <https://www.gnu.org/licenses/>.

"""Management of shape variables."""

from __future__ import annotations

from typing import cast, List, TYPE_CHECKING

import fenics
from mpi4py import MPI
import numpy as np
import petsc4py
from petsc4py import PETSc
from scipy import optimize
from scipy import sparse

from cashocs import _exceptions
from cashocs import _forms
from cashocs import _utils
from cashocs._optimization import optimization_variable_abstractions

if TYPE_CHECKING:
    from cashocs._database import database
    from cashocs._optimization import shape_optimization
    from cashocs._optimization.shape_optimization import mesh_constraints


class ShapeVariableAbstractions(
    optimization_variable_abstractions.OptimizationVariableAbstractions
):
    """Abstractions for optimization variables in the case of shape optimization."""

    def __init__(
        self,
        optimization_problem: shape_optimization.ShapeOptimizationProblem,
        db: database.Database,
        constraint_manager: mesh_constraints.ConstraintManager,
    ) -> None:
        """Initializes self.

        Args:
            optimization_problem: The corresponding optimization problem.
            db: The database of the problem.
            constraint_manager: The constraint manager for mesh quality constraints.

        """
        super().__init__(optimization_problem, db)
        self.form_handler = cast(_forms.ShapeFormHandler, self.form_handler)
        self.mesh_handler = optimization_problem.mesh_handler
        self.constraint_manager = constraint_manager
        self.mode = self.db.config.get("MeshQualityConstraints", "mode")

    def compute_decrease_measure(
        self, search_direction: List[fenics.Function]
    ) -> float:
        """Computes the measure of decrease needed for the Armijo test.

        Args:
            search_direction: The search direction.

        Returns:
            The decrease measure for the Armijo test.

        """
        return self.form_handler.scalar_product(
            self.db.function_db.gradient, search_direction
        )

    def compute_gradient_norm(self) -> float:
        """Computes the norm of the gradient.

        Returns:
            The norm of the gradient.

        """
        res: float = np.sqrt(
            self.form_handler.scalar_product(
                self.db.function_db.gradient, self.db.function_db.gradient
            )
        )
        return res

    def revert_variable_update(self) -> None:
        """Reverts the optimization variables to the current iterate."""
        self.mesh_handler.revert_transformation()

    def update_optimization_variables(
        self, search_direction: List[fenics.Function], stepsize: float, beta: float
    ) -> float:
        """Updates the optimization variables based on a line search.

        Args:
            search_direction: The current search direction.
            stepsize: The current (trial) stepsize.
            beta: The parameter for the line search, which "halves" the stepsize if the
                test was not successful.

        Returns:
            The stepsize which was found to be acceptable.

        """
        while True:
            self.deformation.vector().vec().axpby(
                stepsize, 0.0, search_direction[0].vector().vec()
            )
            self.deformation.vector().apply("")
            if self.mesh_handler.move_mesh(self.deformation):
                if (
                    self.mesh_handler.current_mesh_quality
                    < self.mesh_handler.mesh_quality_tol_lower
                ):
                    stepsize /= beta
                    self.mesh_handler.revert_transformation()
                    continue
                else:
                    break
            else:
                stepsize /= beta

        return stepsize

    def update_constrained_optimization_variables(
        self,
        search_direction: List[fenics.Function],
        stepsize: float,
        beta: float,
        active_idx: np.ndarray[bool],
        constraint_gradient: np.ndarray,
        dropped_idx: np.ndarray,
    ) -> float:
        """Updates the optimization variables based on a line search.

        This variant is used when constraints are present and projects the step back
        to the surface of the constraints in the active set.

        Args:
            search_direction: The current search direction.
            stepsize: The current (trial) stepsize.
            beta: The parameter for the line search, which "halves" the stepsize if the
                test was not successful.
            active_idx: A boolean mask corresponding to the working set.
            constraint_gradient: The gradient of (all) constraints.
            dropped_idx: A boolean mask indicating which constraints have been recently
                dropped from the working set.

        Returns:
            The stepsize which was found to be acceptable.

        """
        while True:
            coords_sequential = self.mesh_handler.mesh.coordinates().copy().reshape(-1)
            coords_dof = coords_sequential[self.constraint_manager.d2v]
            search_direction_vertex = (
                self.mesh_handler.deformation_handler.dof_to_coordinate(
                    search_direction[0]
                )
            )
            search_direction_dof = search_direction_vertex.reshape(-1)[
                self.constraint_manager.d2v
            ]

            if len(active_idx) > 0:
                coords_dof_feasible, stepsize = self.compute_step(
                    coords_dof,
                    search_direction_dof,
                    stepsize,
                    active_idx,
                    constraint_gradient,
                    dropped_idx,
                )

                dof_deformation_vector = coords_dof_feasible - coords_dof
                dof_deformation = fenics.Function(self.db.function_db.control_spaces[0])
                dof_deformation.vector().set_local(dof_deformation_vector)
                dof_deformation.vector().apply("")

                self.deformation.vector().vec().axpby(
                    1.0, 0.0, dof_deformation.vector().vec()
                )
                self.deformation.vector().apply("")
            else:
                self.deformation.vector().vec().axpby(
                    stepsize, 0.0, search_direction[0].vector().vec()
                )
                self.deformation.vector().apply("")

            if self.mesh_handler.move_mesh(self.deformation):
                if (
                    self.mesh_handler.current_mesh_quality
                    < self.mesh_handler.mesh_quality_tol_lower
                ):
                    stepsize /= beta
                    self.mesh_handler.revert_transformation()
                    continue
                else:
                    # ToDo: Check for feasibility, re-project to working set
                    break
            else:
                stepsize /= beta

        return stepsize

    def project_to_working_set(
        self,
        coords_dof: np.ndarray,
        search_direction_dof: np.ndarray,
        stepsize: float,
        active_idx: np.ndarray[bool],
        constraint_gradient: sparse.csr_matrix,
    ) -> np.ndarray | None:
        """Projects an (attempted) step back to the working set of active constraints.

        The step is of the form: `coords_dof + stepsize * search_direction_dof`, the
        working set is defined by `active_idx` and the gradient of (all) constraints is
        given in `constraint_gradient`.

        Args:
            coords_dof: The current coordinates, ordered in a dof-based way.
            search_direction_dof: The search direction, given also in a dof-based way.
            stepsize: The trial size of the step.
            active_idx: A boolean mask used to identify the constraints that are
                currently in the working set.
            constraint_gradient: The sparse matrix containing (all) gradients of the
                constraints.

        Returns:
            The projected step (if the projection was successful) or `None` otherwise.

        """
        comm = self.mesh_handler.mesh.mpi_comm()

        y_j = coords_dof + stepsize * search_direction_dof
        A = self.constraint_manager.compute_active_gradient(
            active_idx, constraint_gradient
        )
        AT = A.copy().transpose()
        B = A.matMult(AT)

        if self.mode == "complete":
            S = self.form_handler.scalar_product_matrix[:, :]
            S_inv = np.linalg.inv(S)

        for i in range(10):
            satisfies_previous_constraints_local = np.all(
                self.constraint_manager.compute_active_set(
                    y_j[self.constraint_manager.v2d]
                )[active_idx]
            )
            satisfies_previous_constraints = comm.allgather(
                satisfies_previous_constraints_local
            )
            satisfies_previous_constraints = np.all(satisfies_previous_constraints)

            if not satisfies_previous_constraints:
                h = self.constraint_manager.evaluate_active(
                    y_j[self.constraint_manager.v2d], active_idx
                )

                if self.mode == "complete":
                    lambd = np.linalg.solve(A @ S_inv @ A.T, h)
                    y_j = y_j - S_inv @ A.T @ lambd
                else:
                    ksp = PETSc.KSP().create(
                        comm=self.constraint_manager.mesh.mpi_comm()
                    )

                    options = {
                        "ksp_type": "cg",
                        "ksp_max_it": 1000,
                        "ksp_rtol": self.constraint_manager.constraint_tolerance / 1e2,
                        "ksp_atol": 1e-30,
                        "pc_type": "hypre",
                        "pc_hypre_type": "boomeramg",
                        # "ksp_monitor_true_residual": None,
                    }

                    ksp.setOperators(B)
                    _utils.setup_petsc_options([ksp], [options])

                    lambd = B.createVecRight()
                    h_petsc = B.createVecLeft()
                    # ToDo: Is this correct? No!
                    # h_petsc.setValuesLocal(np.arange(len(h), dtype="int32"), h)
                    h_petsc.array_w = h
                    h_petsc.assemble()

                    ksp.solve(h_petsc, lambd)

                    if ksp.getConvergedReason() < 0:
                        raise _exceptions.NotConvergedError(
                            "Gradient projection", "The gradient projection failed."
                        )

                    y_petsc = AT.createVecLeft()
                    AT.mult(lambd, y_petsc)

                    update = fenics.Function(self.db.function_db.control_spaces[0])
                    update.vector().vec().aypx(0.0, y_petsc)
                    update.vector().apply("")

                    update_vertex = (
                        self.mesh_handler.deformation_handler.dof_to_coordinate(update)
                    )
                    update_dof = update_vertex.reshape(-1)[self.constraint_manager.d2v]
                    y_j = y_j - update_dof

            else:
                return y_j

        return None

    def compute_step(
        self,
        coords_dof: np.ndarray,
        search_direction_dof: np.ndarray,
        stepsize: float,
        active_idx: np.ndarray[bool],
        constraint_gradient: sparse.csr_matrix,
        dropped_idx: np.ndarray[bool],
    ) -> tuple[np.ndarray, float]:
        """Computes a feasible mesh movement subject to mesh quality constraints.

        Args:
            coords_dof: The current coordinates, ordered in a dof-based way.
            search_direction_dof: The search direction, given also in a dof-based way.
            stepsize: The trial size of the step.
            active_idx: A boolean mask used to identify the constraints that are
                currently in the working set.
            constraint_gradient: The sparse matrix containing (all) gradients of the
                constraints.
            dropped_idx: A boolean mask of indices of constraints, which have been
                dropped from the working set to generate larger descent.

        Returns:
            A tuple `feasible_step, feasible_stepsize`, where `feasible_step` is a
            feasible mesh configuration (based on all constraints) and
            `feasible_stepsize` is the corresponding stepsize taken.

        """
        comm = self.mesh_handler.mesh.mpi_comm()

        def func(lambd):
            projected_step = self.project_to_working_set(
                coords_dof,
                search_direction_dof,
                lambd,
                active_idx,
                constraint_gradient,
            )
            if projected_step is not None:
                rval = np.max(
                    self.constraint_manager.evaluate(
                        projected_step[self.constraint_manager.v2d]
                    )[np.logical_and(~active_idx, ~dropped_idx)]
                )
                value = comm.allreduce(rval, op=MPI.MAX)

                return value
            else:
                return 100.0

        while True:
            self.constraint_manager.comm.barrier()
            trial_step = self.project_to_working_set(
                coords_dof,
                search_direction_dof,
                stepsize,
                active_idx,
                constraint_gradient,
            )
            self.constraint_manager.comm.barrier()
            if trial_step is None:
                stepsize /= 2.0
            else:
                break

        if not np.all(
            self.constraint_manager.is_feasible(trial_step[self.constraint_manager.v2d])
        ):
            feasible_stepsize_lcoal = optimize.root_scalar(
                func, bracket=(0.0, stepsize), xtol=1e-10
            ).root

            feasible_stepsize = comm.allreduce(feasible_stepsize_lcoal, op=MPI.MIN)

            feasible_step = self.project_to_working_set(
                coords_dof,
                search_direction_dof,
                feasible_stepsize,
                active_idx,
                constraint_gradient,
            )

            assert np.all(  # nosec B101
                self.constraint_manager.is_feasible(
                    feasible_step[self.constraint_manager.v2d]
                )
            )
            return feasible_step, feasible_stepsize
        else:
            return trial_step, stepsize

    def compute_a_priori_decreases(
        self, search_direction: List[fenics.Function], stepsize: float
    ) -> int:
        """Computes the number of times the stepsize has to be "halved" a priori.

        Args:
            search_direction: The current search direction.
            stepsize: The current stepsize.

        Returns:
            The number of times the stepsize has to be "halved" before the actual trial.

        """
        return self.mesh_handler.compute_decreases(search_direction, stepsize)

    def requires_remeshing(self) -> bool:
        """Checks, if remeshing is needed.

        Returns:
            A boolean, which indicates whether remeshing is required.

        """
        mesh_quality_criterion = bool(
            self.mesh_handler.current_mesh_quality
            < self.mesh_handler.mesh_quality_tol_upper
        )

        iteration = self.db.parameter_db.optimization_state["iteration"]
        if self.db.config.getint("MeshQuality", "remesh_iter") > 0:
            iteration_criterion = bool(
                iteration > 0
                and iteration % self.db.config.getint("MeshQuality", "remesh_iter") == 0
            )
        else:
            iteration_criterion = False

        requires_remeshing = mesh_quality_criterion or iteration_criterion
        return requires_remeshing

    def project_ncg_search_direction(
        self, search_direction: List[fenics.Function]
    ) -> None:
        """Restricts the search direction to the inactive set.

        Args:
            search_direction: The current search direction (will be overwritten).

        """
        pass
