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

"""Database for parameters."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cashocs import _utils

if TYPE_CHECKING:
    from cashocs import _typing
    from cashocs import io
    from cashocs._database import function_database


class ParameterDatabase:
    """A database for many parameters."""

    def __init__(
        self,
        function_db: function_database.FunctionDatabase,
        config: io.Config,
        state_ksp_options: _typing.KspOptions,
        adjoint_ksp_options: _typing.KspOptions,
    ) -> None:
        """Initializes the database.

        Args:
            function_db: The database for functions.
            config: The configuration.
            state_ksp_options: The list of ksp options for the state system.
            adjoint_ksp_options: The list of ksp options for the adjoint system.

        """
        self.config = config
        self.state_ksp_options = state_ksp_options
        self.adjoint_ksp_options = adjoint_ksp_options

        self.state_dim = len(function_db.states)

        self.state_adjoint_equal_spaces = False
        if function_db.state_spaces == function_db.adjoint_spaces:
            self.state_adjoint_equal_spaces = True

        self.state_is_linear = self.config.getboolean("StateSystem", "is_linear")
        self.state_is_picard = self.config.getboolean("StateSystem", "picard_iteration")
        self.opt_algo: str = _utils.optimization_algorithm_configuration(self.config)
