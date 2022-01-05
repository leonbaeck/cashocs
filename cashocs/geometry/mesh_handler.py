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

from __future__ import annotations

import configparser
import json
import os
import subprocess
import sys
import tempfile
from typing import List, TYPE_CHECKING

import fenics
import numpy as np
from petsc4py import PETSc

from .deformation_handler import DeformationHandler
from .mesh_quality import compute_mesh_quality
from .._exceptions import CashocsException, InputError, IncompatibleConfigurationError
from .._loggers import debug, warning
from ..io import write_out_mesh
from ..utils.linalg import (
    _assemble_petsc_system,
    _setup_petsc_options,
    _solve_linear_problem,
)


if TYPE_CHECKING:
    from .._shape_optimization.shape_optimization_problem import (
        ShapeOptimizationProblem,
    )
    from .._shape_optimization.shape_optimization_algorithm import (
        ShapeOptimizationAlgorithm,
    )


class _MeshHandler:
    """Handles the mesh for shape optimization problems.

    This class implements all mesh related things for the shape optimization,
     such as transformations and remeshing. Also includes mesh quality control
     checks.
    """

    def __init__(self, shape_optimization_problem: ShapeOptimizationProblem) -> None:
        """Initializes the MeshHandler object.

        Parameters
        ----------
        shape_optimization_problem : ShapeOptimizationProblem
            The corresponding shape optimization problem.
        """

        self.form_handler = shape_optimization_problem.form_handler
        # Namespacing
        self.mesh = self.form_handler.mesh
        self.deformation_handler = DeformationHandler(self.mesh)
        self.dx = self.form_handler.dx
        self.bbtree = self.mesh.bounding_box_tree()
        self.config = self.form_handler.config

        # setup from config
        self.volume_change = float(
            self.config.get("MeshQuality", "volume_change", fallback="inf")
        )
        self.angle_change = float(
            self.config.get("MeshQuality", "angle_change", fallback="inf")
        )

        self.mesh_quality_tol_lower = self.config.getfloat(
            "MeshQuality", "tol_lower", fallback=0.0
        )
        self.mesh_quality_tol_upper = self.config.getfloat(
            "MeshQuality", "tol_upper", fallback=1e-15
        )
        if not self.mesh_quality_tol_lower < self.mesh_quality_tol_upper:
            raise IncompatibleConfigurationError(
                "tol_lower",
                "MeshQuality",
                "tol_upper",
                "MeshQuality",
                "Reason: tol_lower has to be strictly smaller than tol_upper",
            )

        if self.mesh_quality_tol_lower > 0.9 * self.mesh_quality_tol_upper:
            warning(
                "You are using a lower remesh tolerance (tol_lower) close to the upper one (tol_upper). This may slow down the optimization considerably."
            )

        self.mesh_quality_measure = self.config.get(
            "MeshQuality", "measure", fallback="skewness"
        )

        self.mesh_quality_type = self.config.get("MeshQuality", "type", fallback="min")

        self.current_mesh_quality = 1.0
        self.current_mesh_quality = compute_mesh_quality(
            self.mesh, self.mesh_quality_type, self.mesh_quality_measure
        )

        self.__setup_decrease_computation()
        self.__setup_a_priori()

        # Remeshing initializations
        self.do_remesh = self.config.getboolean("Mesh", "remesh", fallback=False)
        self.save_optimized_mesh = self.config.getboolean(
            "Output", "save_mesh", fallback=False
        )

        if self.do_remesh or self.save_optimized_mesh:
            try:
                self.mesh_directory = os.path.dirname(
                    os.path.realpath(self.config.get("Mesh", "gmsh_file"))
                )
            except configparser.Error:
                if self.do_remesh:
                    raise IncompatibleConfigurationError(
                        "gmsh_file",
                        "Mesh",
                        "remesh",
                        "Mesh",
                        "Reason: Remeshing is only available with gmsh meshes. Please specify gmsh_file.",
                    )
                elif self.save_optimized_mesh:
                    raise IncompatibleConfigurationError(
                        "save_mesh",
                        "Mesh",
                        "gmsh_file",
                        "Mesh",
                        "Reason: The config option OptimizationRoutine.save_mesh is only available for gmsh meshes. \n"
                        "If you already use a gmsh mesh, please specify gmsh_file.",
                    )

        if self.do_remesh:
            self.temp_dict = shape_optimization_problem.temp_dict
            self.gmsh_file = self.temp_dict["gmsh_file"]
            self.remesh_counter = self.temp_dict.get("remesh_counter", 0)

            if not self.form_handler.has_cashocs_remesh_flag:
                self.remesh_directory = tempfile.mkdtemp(
                    prefix="cashocs_remesh_", dir=self.mesh_directory
                )
            else:
                self.remesh_directory = self.temp_dict["remesh_directory"]
            if not os.path.isdir(os.path.realpath(self.remesh_directory)):
                os.mkdir(self.remesh_directory)
            self.remesh_geo_file = f"{self.remesh_directory}/remesh.geo"

        elif self.save_optimized_mesh:
            self.gmsh_file = self.config.get("Mesh", "gmsh_file")

        # create a copy of the initial mesh file
        if self.do_remesh and self.remesh_counter == 0:
            self.gmsh_file_init = (
                f"{self.remesh_directory}/mesh_{self.remesh_counter:d}.msh"
            )
            subprocess.run(["cp", self.gmsh_file, self.gmsh_file_init], check=True)
            self.gmsh_file = self.gmsh_file_init

    def move_mesh(self, transformation: fenics.Function) -> None:
        r"""Transforms the mesh by perturbation of identity.

        Moves the mesh according to the deformation given by

        .. math:: \text{id} + \mathcal{V}(x),

        where :math:`\mathcal{V}` is the transformation. This
        represents the perturbation of identity.

        Parameters
        ----------
        transformation : fenics.Function
            The transformation for the mesh, a vector CG1 Function.

        Returns
        -------
        None
        """

        if not (
            transformation.ufl_element().family() == "Lagrange"
            and transformation.ufl_element().degree() == 1
        ):
            raise CashocsException("Not a valid mesh transformation")

        if not self.__test_a_priori(transformation):
            debug("Mesh transformation rejected due to a priori check.")
            return False
        else:
            success_flag = self.deformation_handler.move_mesh(
                transformation, validated_a_priori=True
            )
            self.current_mesh_quality = compute_mesh_quality(
                self.mesh, self.mesh_quality_type, self.mesh_quality_measure
            )
            return success_flag

    def revert_transformation(self) -> None:
        """Reverts the previous mesh transformation.

        This is used when the mesh quality for the resulting deformed mesh
        is not sufficient, or when the solution algorithm terminates, e.g., due
        to lack of sufficient decrease in the Armijo rule

        Returns
        -------
        None
        """

        self.deformation_handler.revert_transformation()

    def __setup_decrease_computation(self) -> None:
        """Initializes attributes and solver for the frobenius norm check.

        Returns
        -------
        None
        """

        self.options_frobenius = [
            ["ksp_type", "preonly"],
            ["pc_type", "jacobi"],
            ["pc_jacobi_type", "diagonal"],
            ["ksp_rtol", 1e-16],
            ["ksp_atol", 1e-20],
            ["ksp_max_it", 1000],
        ]
        self.ksp_frobenius = PETSc.KSP().create()
        _setup_petsc_options([self.ksp_frobenius], [self.options_frobenius])

        self.trial_dg0 = fenics.TrialFunction(self.form_handler.DG0)
        self.test_dg0 = fenics.TestFunction(self.form_handler.DG0)

        if not (self.angle_change == float("inf")):
            self.search_direction_container = fenics.Function(
                self.form_handler.deformation_space
            )

            self.a_frobenius = self.trial_dg0 * self.test_dg0 * self.dx
            self.L_frobenius = (
                fenics.sqrt(
                    fenics.inner(
                        fenics.grad(self.search_direction_container),
                        fenics.grad(self.search_direction_container),
                    )
                )
                * self.test_dg0
                * self.dx
            )

    def compute_decreases(
        self, search_direction: List[fenics.Function], stepsize: float
    ) -> int:
        """Estimates the number of Armijo decreases for a certain mesh quality.

        Gives a better estimation of the stepsize. The output is
        the number of Armijo decreases we have to do in order to
        get a transformation that satisfies norm(transformation)_fro <= tol,
        where transformation = stepsize*search_direction and tol is specified in
        the config file under "angle_change". Due to the linearity
        of the norm this has to be done only once, all smaller stepsizes are
        feasible w.r.t. this criterion as well.

        Parameters
        ----------
        search_direction : list[fenics.Function]
            The search direction in the optimization routine / descent algorithm.
        stepsize : float
            The stepsize in the descent algorithm.

        Returns
        -------
        int
            A guess for the number of "Armijo halvings" to get a better stepsize
        """

        if self.angle_change == float("inf"):
            return 0

        else:
            self.search_direction_container.vector().vec().aypx(
                0.0, search_direction[0].vector().vec()
            )
            A, b = _assemble_petsc_system(self.a_frobenius, self.L_frobenius)
            x = _solve_linear_problem(
                self.ksp_frobenius, A, b, ksp_options=self.options_frobenius
            )

            frobenius_norm = np.max(x[:])
            beta_armijo = self.config.getfloat(
                "OptimizationRoutine", "beta_armijo", fallback=2
            )

            return int(
                np.maximum(
                    np.ceil(
                        np.log(self.angle_change / stepsize / frobenius_norm)
                        / np.log(1 / beta_armijo)
                    ),
                    0.0,
                )
            )

    def __setup_a_priori(self) -> None:
        """Sets up the attributes and petsc solver for the a priori quality check.

        Returns
        -------
        None
        """

        self.options_prior = [
            ["ksp_type", "preonly"],
            ["pc_type", "jacobi"],
            ["pc_jacobi_type", "diagonal"],
            ["ksp_rtol", 1e-16],
            ["ksp_atol", 1e-20],
            ["ksp_max_it", 1000],
        ]
        self.ksp_prior = PETSc.KSP().create()
        _setup_petsc_options([self.ksp_prior], [self.options_prior])

        self.transformation_container = fenics.Function(
            self.form_handler.deformation_space
        )
        dim = self.mesh.geometric_dimension()

        self.a_prior = self.trial_dg0 * self.test_dg0 * self.dx
        self.L_prior = (
            fenics.det(
                fenics.Identity(dim) + fenics.grad(self.transformation_container)
            )
            * self.test_dg0
            * self.dx
        )

    def __test_a_priori(self, transformation: fenics.Function) -> bool:
        r"""Check the quality of the transformation before the actual mesh is moved.

        Checks the quality of the transformation. The criterion is that

        .. math:: \det(I + D \texttt{transformation})

        should neither be too large nor too small in order to achieve the best
        transformations.

        Parameters
        ----------
        transformation : fenics.Function
            The transformation for the mesh.

        Returns
        -------
        bool
            A boolean that indicates whether the desired transformation is feasible
        """

        self.transformation_container.vector().vec().aypx(
            0.0, transformation.vector().vec()
        )
        A, b = _assemble_petsc_system(self.a_prior, self.L_prior)
        x = _solve_linear_problem(self.ksp_prior, A, b, ksp_options=self.options_prior)

        min_det = np.min(x[:])
        max_det = np.max(x[:])

        return (min_det >= 1 / self.volume_change) and (max_det <= self.volume_change)

    def __generate_remesh_geo(self, input_mesh_file: str) -> None:
        """Generates a .geo file used for remeshing.

        The .geo file is generated via the original .geo file for the
        initial geometry, so that mesh size fields are correctly given
        for the remeshing.

        Parameters
        ----------
        input_mesh_file : str
            Path to the mesh file used for generating the new .geo file

        Returns
        -------
        None
        """

        with open(self.remesh_geo_file, "w") as file:
            temp_name = os.path.split(input_mesh_file)[1]

            file.write(f"Merge '{temp_name}';\n")
            file.write("CreateGeometry;\n")
            file.write("\n")

            geo_file = self.temp_dict["geo_file"]
            with open(geo_file, "r") as f:
                for line in f:
                    if line[0].islower():
                        # if line[:2] == 'lc':
                        file.write(line)
                    if line[:5] == "Field":
                        file.write(line)
                    if line[:16] == "Background Field":
                        file.write(line)
                    if line[:19] == "BoundaryLayer Field":
                        file.write(line)
                    if line[:5] == "Mesh.":
                        file.write(line)

    def __remove_gmsh_parametrizations(self, mesh_file: str) -> None:
        """Removes the parametrizations section from a Gmsh file.

        This is needed in case several remeshing iterations have to be
        executed.

        Parameters
        ----------
        mesh_file : str
            Path to the Gmsh file.

        Returns
        -------
        None
        """

        if not mesh_file[-4:] == ".msh":
            raise InputError(
                "cashocs.geometry.__remove_gmsh_parametrizations",
                "mesh_file",
                "Format for mesh_file is wrong, has to end in .msh",
            )

        temp_location = f"{mesh_file[:-4]}_temp.msh"

        with open(mesh_file, "r") as in_file, open(temp_location, "w") as temp_file:

            parametrizations_section = False

            for line in in_file:

                if line == "$Parametrizations\n":
                    parametrizations_section = True

                if not parametrizations_section:
                    temp_file.write(line)
                else:
                    pass

                if line == "$EndParametrizations\n":
                    parametrizations_section = False

        subprocess.run(["mv", temp_location, mesh_file], check=True)

    def clean_previous_gmsh_files(self) -> None:
        """Removes the gmsh files from the previous remeshing iterations to save disk space

        Returns
        -------
        None
        """

        gmsh_file = f"{self.remesh_directory}/mesh_{self.remesh_counter - 1:d}.msh"
        if os.path.isfile(gmsh_file):
            subprocess.run(["rm", gmsh_file], check=True)

        gmsh_pre_remesh_file = (
            f"{self.remesh_directory}/mesh_{self.remesh_counter-1:d}_pre_remesh.msh"
        )
        if os.path.isfile(gmsh_pre_remesh_file):
            subprocess.run(["rm", gmsh_pre_remesh_file], check=True)

        mesh_h5_file = f"{self.remesh_directory}/mesh_{self.remesh_counter-1:d}.h5"
        if os.path.isfile(mesh_h5_file):
            subprocess.run(["rm", mesh_h5_file], check=True)

        mesh_xdmf_file = f"{self.remesh_directory}/mesh_{self.remesh_counter-1:d}.xdmf"
        if os.path.isfile(mesh_xdmf_file):
            subprocess.run(["rm", mesh_xdmf_file], check=True)

        boundaries_h5_file = (
            f"{self.remesh_directory}/mesh_{self.remesh_counter-1:d}_boundaries.h5"
        )
        if os.path.isfile(boundaries_h5_file):
            subprocess.run(["rm", boundaries_h5_file], check=True)

        boundaries_xdmf_file = (
            f"{self.remesh_directory}/mesh_{self.remesh_counter-1:d}_boundaries.xdmf"
        )
        if os.path.isfile(boundaries_xdmf_file):
            subprocess.run(["rm", boundaries_xdmf_file], check=True)

        subdomains_h5_file = (
            f"{self.remesh_directory}/mesh_{self.remesh_counter-1:d}_subdomains.h5"
        )
        if os.path.isfile(subdomains_h5_file):
            subprocess.run(["rm", subdomains_h5_file], check=True)

        subdomains_xdmf_file = (
            f"{self.remesh_directory}/mesh_{self.remesh_counter-1:d}_subdomains.xdmf"
        )
        if os.path.isfile(subdomains_xdmf_file):
            subprocess.run(["rm", subdomains_xdmf_file], check=True)

    def remesh(self, solver: ShapeOptimizationAlgorithm):
        """Remeshes the current geometry with GMSH.

        Performs a remeshing of the geometry, and then restarts
        the optimization problem with the new mesh.

        Parameters
        ----------
        solver : ShapeOptimizationAlgorithm
            The optimization algorithm used to solve the problem.

        Returns
        -------
        None
        """

        if self.do_remesh:
            self.remesh_counter += 1
            self.temp_file = (
                f"{self.remesh_directory}/mesh_{self.remesh_counter:d}_pre_remesh.msh"
            )
            write_out_mesh(self.mesh, self.gmsh_file, self.temp_file)
            self.__generate_remesh_geo(self.temp_file)

            # save the output dict (without the last entries since they are "remeshed")
            self.temp_dict["output_dict"] = {}
            self.temp_dict["output_dict"][
                "state_solves"
            ] = solver.state_problem.number_of_solves
            self.temp_dict["output_dict"][
                "adjoint_solves"
            ] = solver.adjoint_problem.number_of_solves
            self.temp_dict["output_dict"]["iterations"] = solver.iteration + 1

            output_dict = solver.output_manager.result_manager.output_dict
            self.temp_dict["output_dict"]["cost_function_value"] = output_dict[
                "cost_function_value"
            ][:]
            self.temp_dict["output_dict"]["gradient_norm"] = output_dict[
                "gradient_norm"
            ][:]
            self.temp_dict["output_dict"]["stepsize"] = output_dict["stepsize"][:]
            self.temp_dict["output_dict"]["MeshQuality"] = output_dict["MeshQuality"][:]

            dim = self.mesh.geometric_dimension()

            self.new_gmsh_file = (
                f"{self.remesh_directory}/mesh_{self.remesh_counter:d}.msh"
            )

            gmsh_cmd_list = [
                "gmsh",
                self.remesh_geo_file,
                f"-{int(dim):d}",
                "-o",
                self.new_gmsh_file,
            ]
            if not self.config.getboolean("Mesh", "show_gmsh_output", fallback=False):
                subprocess.run(
                    gmsh_cmd_list,
                    check=True,
                    stdout=subprocess.DEVNULL,
                )
            else:
                subprocess.run(gmsh_cmd_list, check=True)

            self.__remove_gmsh_parametrizations(self.new_gmsh_file)

            self.temp_dict["remesh_counter"] = self.remesh_counter
            self.temp_dict["remesh_directory"] = self.remesh_directory
            self.temp_dict["result_dir"] = solver.output_manager.result_dir

            self.new_xdmf_file = (
                f"{self.remesh_directory}/mesh_{self.remesh_counter:d}.xdmf"
            )

            subprocess.run(
                ["cashocs-convert", self.new_gmsh_file, self.new_xdmf_file],
                check=True,
            )

            self.clean_previous_gmsh_files()

            self.temp_dict["mesh_file"] = self.new_xdmf_file
            self.temp_dict["gmsh_file"] = self.new_gmsh_file

            self.temp_dict["OptimizationRoutine"]["iteration_counter"] = (
                solver.iteration + 1
            )
            self.temp_dict["OptimizationRoutine"][
                "gradient_norm_initial"
            ] = solver.gradient_norm_initial

            self.temp_dir = self.temp_dict["temp_dir"]

            with open(f"{self.temp_dir}/temp_dict.json", "w") as file:
                json.dump(self.temp_dict, file)

            def filter_sys_argv():
                """Filters the command line arguments for the cashocs remesh flag

                Returns
                -------
                list[str]
                    The filtered list of command line arguments
                """
                arg_list = sys.argv.copy()
                idx_cashocs_remesh_flag = [
                    i for i, s in enumerate(arg_list) if s == "--cashocs_remesh"
                ]
                if len(idx_cashocs_remesh_flag) > 1:
                    raise InputError(
                        "Command line options",
                        "--cashocs_remesh",
                        "The --cashocs_remesh flag should only be present once.",
                    )
                elif len(idx_cashocs_remesh_flag) == 1:
                    arg_list.pop(idx_cashocs_remesh_flag[0])

                idx_temp_dir = [i for i, s in enumerate(arg_list) if s == self.temp_dir]
                if len(idx_temp_dir) > 1:
                    raise InputError(
                        "Command line options",
                        "--temp_dir",
                        "The --temp_dir flag should only be present once.",
                    )
                elif len(idx_temp_dir) == 1:
                    arg_list.pop(idx_temp_dir[0])

                idx_temp_dir_flag = [
                    i for i, s in enumerate(arg_list) if s == "--temp_dir"
                ]
                if len(idx_temp_dir) > 1:
                    raise InputError(
                        "Command line options",
                        "--temp_dir",
                        "The --temp_dir flag should only be present once.",
                    )
                elif len(idx_temp_dir_flag) == 1:
                    arg_list.pop(idx_temp_dir_flag[0])

                return arg_list

            if not self.form_handler.has_cashocs_remesh_flag:
                os.execv(
                    sys.executable,
                    [sys.executable]
                    + filter_sys_argv()
                    + ["--cashocs_remesh"]
                    + ["--temp_dir"]
                    + [self.temp_dir],
                )
            else:
                os.execv(
                    sys.executable,
                    [sys.executable]
                    + filter_sys_argv()
                    + ["--cashocs_remesh"]
                    + ["--temp_dir"]
                    + [self.temp_dir],
                )
