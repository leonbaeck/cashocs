# Copyright (C) 2020-2024 Sebastian Blauth
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

"""Interface for the PETSc SNES solver for nonlinear equations."""

from __future__ import annotations

import copy
from typing import List, Optional, TYPE_CHECKING, Union

import fenics
from petsc4py import PETSc

try:
    import ufl_legacy as ufl
except ImportError:
    import ufl

from cashocs import _utils

if TYPE_CHECKING:
    from cashocs import _typing


default_snes_options = {
    "snes_type": "newtonls",
    "snes_monitor_short": None,
}


class SNESSolver:
    """Interface for using PETSc's SNES solver."""

    def __init__(
        self,
        nonlinear_form: ufl.Form,
        u: fenics.Function,
        bcs: Union[fenics.DirichletBC, List[fenics.DirichletBC]],
        derivative: Optional[ufl.Form] = None,
        petsc_options: Optional[_typing.KspOption] = None,
        A_tensor: Optional[fenics.PETScMatrix] = None,  # pylint: disable=invalid-name
        b_tensor: Optional[fenics.PETScVector] = None,
        preconditioner_form: Optional[ufl.Form] = None,
    ) -> None:
        """Initialize the SNES solver.

        Args:
            nonlinear_form: The variational form of the nonlinear problem to be solved
                by Newton's method.
            u: The sought solution / initial guess. It is not assumed that the initial
                guess satisfies the Dirichlet boundary conditions, they are applied
                automatically. The method overwrites / updates this Function.
            bcs: A list of DirichletBCs for the nonlinear variational problem.
            derivative: The Jacobian of nonlinear_form, used for the Newton method.
                Default is None, and in this case the Jacobian is computed automatically
                with AD.
            petsc_options: The options for PETSc.
            A_tensor: A fenics.PETScMatrix for storing the left-hand side of the linear
                sub-problem.
            b_tensor: A fenics.PETScVector for storing the right-hand side of the linear
                sub-problem.
            preconditioner_form: A UFL form which defines the preconditioner matrix.

        """
        self.nonlinear_form = nonlinear_form
        self.u = u
        self.comm = self.u.function_space().mesh().mpi_comm()
        self.bcs = _utils.enlist(bcs)

        if petsc_options is None:
            self.petsc_options = copy.deepcopy(default_snes_options)
            self.petsc_options.update(_utils.linalg.direct_ksp_options)  # type: ignore
        else:
            self.petsc_options = petsc_options  # type: ignore

        self.A_tensor = A_tensor  # pylint: disable=invalid-name
        self.b_tensor = b_tensor

        if preconditioner_form is not None:
            if len(preconditioner_form.arguments()) == 1:
                self.preconditioner_form = fenics.derivative(
                    preconditioner_form, self.u
                )
            else:
                self.preconditioner_form = preconditioner_form
        else:
            self.preconditioner_form = None

        temp_derivative = derivative or fenics.derivative(self.nonlinear_form, self.u)
        self.derivative = _utils.bilinear_boundary_form_modification([temp_derivative])[
            0
        ]

        self.assembler = fenics.SystemAssembler(
            self.derivative, self.nonlinear_form, self.bcs
        )
        self.assembler.keep_diagonal = True

        if self.preconditioner_form is not None:
            self.assembler_pc = fenics.SystemAssembler(
                self.preconditioner_form, self.nonlinear_form, self.bcs
            )
            self.assembler_pc.keep_diagonal = True

        self.A_fenics = (  # pylint: disable=invalid-name
            self.A_tensor or fenics.PETScMatrix(self.comm)
        )
        self.residual = self.b_tensor or fenics.PETScVector(self.comm)
        self.b = fenics.as_backend_type(self.residual).vec()

        self.P_fenics = fenics.PETScMatrix(self.comm)  # pylint: disable=invalid-name

    def assemble_function(
        self,
        snes: PETSc.SNES,  # pylint: disable=unused-argument
        x: PETSc.Vec,
        f: PETSc.Vec,
    ) -> None:
        """Interface for PETSc SNESSetFunction.

        Args:
            snes: The SNES solver
            x: The current iterate
            f: The vector in which the function evaluation is stored.

        """
        self.u.vector().vec().setArray(x)
        f = fenics.PETScVector(f)

        self.assembler.assemble(f, self.u.vector())

    def assemble_jacobian(
        self,
        snes: PETSc.SNES,  # pylint: disable=unused-argument
        x: PETSc.Vec,
        J: PETSc.Mat,  # pylint: disable=invalid-name,
        P: PETSc.Mat,  # pylint: disable=invalid-name
    ) -> None:
        """Interface for PETSc SNESSetJacobian.

        Args:
            snes: The SNES solver.
            x: The current iterate.
            J: The matrix storing the Jacobian.
            P: The matrix storing the preconditioner for the Jacobian.

        """
        self.u.vector().vec().setArray(x)
        J = fenics.PETScMatrix(J)  # pylint: disable=invalid-name
        P = fenics.PETScMatrix(P)  # pylint: disable=invalid-name

        self.assembler.assemble(J)
        J.ident_zeros()

        if self.preconditioner_form is not None:
            self.assembler_pc.assemble(P)
            P.ident_zeros()

    def solve(self) -> fenics.Function:
        """Solves the nonlinear problem with PETSc's SNES."""
        snes = PETSc.SNES().create()

        snes.setFunction(self.assemble_function, self.residual.vec())
        snes.setJacobian(
            self.assemble_jacobian, self.A_fenics.mat(), self.P_fenics.mat()
        )

        _utils.setup_petsc_options([snes], [self.petsc_options])  # type: ignore
        snes.solve(None, self.u.vector().vec())

        return self.u


def snes_solve(
    nonlinear_form: ufl.Form,
    u: fenics.Function,
    bcs: Union[fenics.DirichletBC, List[fenics.DirichletBC]],
    derivative: Optional[ufl.Form] = None,
    petsc_options: Optional[_typing.KspOption] = None,
    A_tensor: Optional[fenics.PETScMatrix] = None,  # pylint: disable=invalid-name
    b_tensor: Optional[fenics.PETScVector] = None,
    preconditioner_form: Optional[ufl.Form] = None,
) -> fenics.Function:
    """Solve a nonlinear PDE problem with PETSc SNES.

    Args:
        nonlinear_form: The variational form of the nonlinear problem to be solved
            by Newton's method.
        u: The sought solution / initial guess. It is not assumed that the initial
            guess satisfies the Dirichlet boundary conditions, they are applied
            automatically. The method overwrites / updates this Function.
        bcs: A list of DirichletBCs for the nonlinear variational problem.
        derivative: The Jacobian of nonlinear_form, used for the Newton method.
            Default is None, and in this case the Jacobian is computed automatically
            with AD.
        petsc_options: The options for PETSc.
        A_tensor: A fenics.PETScMatrix for storing the left-hand side of the linear
            sub-problem.
        b_tensor: A fenics.PETScVector for storing the right-hand side of the linear
            sub-problem.
        preconditioner_form: A UFL form which defines the preconditioner matrix.

    Returns:
        The solution in form of a FEniCS Function.

    """
    solver = SNESSolver(
        nonlinear_form,
        u,
        bcs,
        derivative,
        petsc_options,
        A_tensor,
        b_tensor,
        preconditioner_form,
    )

    solution = solver.solve()
    return solution
