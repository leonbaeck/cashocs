# Copyright (C) 2020-2022 Sebastian Blauth
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

"""Several PDE problems for optimization (shape and optimal control).

"""

from .adjoint_problem import AdjointProblem
from .control_gradient_problem import ControlGradientProblem
from .hessian_problems import HessianProblem
from .pde_problem import PDEProblem
from .shape_gradient_problem import ShapeGradientProblem
from .state_problem import StateProblem


__all__ = [
    "PDEProblem",
    "AdjointProblem",
    "ControlGradientProblem",
    "HessianProblem",
    "ShapeGradientProblem",
    "StateProblem",
]
