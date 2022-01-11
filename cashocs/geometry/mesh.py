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
import time
from typing import Union, Optional, Tuple, TYPE_CHECKING

import fenics
import numpy as np
from typing_extensions import Literal

from .measure import _NamedMeasure
from .mesh_quality import compute_mesh_quality
from .._exceptions import InputError
from .._loggers import info, warning
from ..utils.helpers import (
    _parse_remesh,
)


if TYPE_CHECKING:
    pass


class Mesh(fenics.Mesh):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._config_flag = False

    def _set_config_flag(self) -> None:
        self._config_flag = True


def import_mesh(
    input_arg: Union[str, configparser.ConfigParser]
) -> Tuple[
    Mesh,
    fenics.MeshFunction,
    fenics.MeshFunction,
    fenics.Measure,
    fenics.Measure,
    fenics.Measure,
]:
    """Imports a mesh file for use with cashocs / FEniCS.

    This function imports a mesh file that was generated by GMSH and converted to
    .xdmf with the command line function :ref:`cashocs-convert <cashocs_convert>`.
    If there are Physical quantities specified in the GMSH file, these are imported
    to the subdomains and boundaries output of this function and can also be directly
    accessed via the measures, e.g., with ``dx(1)``, ``ds(1)``, etc.

    Parameters
    ----------
    input_arg : str or configparser.ConfigParser
        This is either a string, in which case it corresponds to the location
        of the mesh file in .xdmf file format, or a config file that
        has this path stored in its settings, under the section Mesh, as
        parameter ``mesh_file``.

    Returns
    -------
    mesh : Mesh
        The imported (computational) mesh.
    subdomains : fenics.MeshFunction
        A :py:class:`fenics.MeshFunction` object containing the subdomains,
        i.e., the Physical regions marked in the GMSH file.
    boundaries : fenics.MeshFunction
        A MeshFunction object containing the boundaries,
        i.e., the Physical regions marked in the GMSH file. Can, e.g., be used to set
        up boundary conditions.
    dx : fenics.Measure
        The volume measure of the mesh corresponding to
        the subdomains (i.e. GMSH Physical region indices).
    ds : fenics.Measure
        The surface measure of the mesh corresponding to
        the boundaries (i.e. GMSH Physical region indices).
    dS : fenics.Measure
        The interior facet measure of the mesh corresponding
        to boundaries (i.e. GMSH Physical region indices).

    Notes
    -----
    In case the boundaries in the Gmsh .msh file are not only marked with numbers (as pyhsical
    groups), but also with names (i.e. strings), these strings can be used with the integration
    measures ``dx`` and ``ds`` returned by this method. E.g., if one specified the
    following in a 2D Gmsh .geo file ::

        Physical Surface("domain", 1) = {i,j,k};

    where i,j,k are representative for some integers, then this can be used in the measure
    ``dx`` (as we are 2D) as follows. The command ::

        dx(1)

    is completely equivalent to ::

       dx("domain")

    and both can be used interchangeably.
    """

    start_time = time.time()
    info("Importing mesh.")

    cashocs_remesh_flag, temp_dir = _parse_remesh()

    # Check for the file format
    if isinstance(input_arg, str):
        mesh_file = input_arg
    elif isinstance(input_arg, configparser.ConfigParser):
        ### overloading for remeshing
        if not input_arg.getboolean("Mesh", "remesh", fallback=False):
            mesh_file = input_arg.get("Mesh", "mesh_file")
        else:
            if not cashocs_remesh_flag:
                mesh_file = input_arg.get("Mesh", "mesh_file")
            else:
                with open(f"{temp_dir}/temp_dict.json", "r") as file:
                    temp_dict = json.load(file)
                mesh_file = temp_dict["mesh_file"]

    else:
        raise InputError(
            "cashocs.geometry.import_mesh",
            "input_arg",
            "Not a valid argument for import_mesh. Has to be either a path to a mesh file (str) or a config.",
        )

    file_string = mesh_file[:-5]

    mesh = Mesh()
    xdmf_file = fenics.XDMFFile(mesh.mpi_comm(), mesh_file)
    xdmf_file.read(mesh)
    xdmf_file.close()

    subdomains_mvc = fenics.MeshValueCollection(
        "size_t", mesh, mesh.geometric_dimension()
    )
    boundaries_mvc = fenics.MeshValueCollection(
        "size_t", mesh, mesh.geometric_dimension() - 1
    )

    if os.path.isfile(f"{file_string}_subdomains.xdmf"):
        xdmf_subdomains = fenics.XDMFFile(
            mesh.mpi_comm(), f"{file_string}_subdomains.xdmf"
        )
        xdmf_subdomains.read(subdomains_mvc, "subdomains")
        xdmf_subdomains.close()
    if os.path.isfile(f"{file_string}_boundaries.xdmf"):
        xdmf_boundaries = fenics.XDMFFile(
            mesh.mpi_comm(), f"{file_string}_boundaries.xdmf"
        )
        xdmf_boundaries.read(boundaries_mvc, "boundaries")
        xdmf_boundaries.close()

    physical_groups = None
    if os.path.isfile(f"{file_string}_physical_groups.json"):
        with open(f"{file_string}_physical_groups.json") as file:
            physical_groups = json.load(file)

    subdomains = fenics.MeshFunction("size_t", mesh, subdomains_mvc)
    boundaries = fenics.MeshFunction("size_t", mesh, boundaries_mvc)

    dx = _NamedMeasure(
        "dx", domain=mesh, subdomain_data=subdomains, physical_groups=physical_groups
    )
    ds = _NamedMeasure(
        "ds", domain=mesh, subdomain_data=boundaries, physical_groups=physical_groups
    )
    dS = _NamedMeasure(
        "dS", domain=mesh, subdomain_data=boundaries, physical_groups=physical_groups
    )

    end_time = time.time()
    info(f"Done importing mesh. Elapsed time: {end_time - start_time:.2f} s")
    info(
        f"Mesh contains {mesh.num_vertices():,} vertices and {mesh.num_cells():,} cells of type {mesh.ufl_cell().cellname()}.\n"
    )

    # Add an attribute to the mesh to show with what procedure it was generated
    mesh._set_config_flag()
    # Add the physical groups to the mesh in case they are present
    if physical_groups is not None:
        mesh._physical_groups = physical_groups

    # Check the mesh quality of the imported mesh in case a config file is passed
    if isinstance(input_arg, configparser.ConfigParser):
        mesh_quality_tol_lower = input_arg.getfloat(
            "MeshQuality", "tol_lower", fallback=0.0
        )
        mesh_quality_tol_upper = input_arg.getfloat(
            "MeshQuality", "tol_upper", fallback=1e-15
        )

        if mesh_quality_tol_lower > 0.9 * mesh_quality_tol_upper:
            warning(
                "You are using a lower remesh tolerance (tol_lower) close to the upper one (tol_upper). This may slow down the optimization considerably."
            )

        mesh_quality_measure = input_arg.get(
            "MeshQuality", "measure", fallback="skewness"
        )
        mesh_quality_type = input_arg.get("MeshQuality", "type", fallback="min")

        current_mesh_quality = compute_mesh_quality(
            mesh, mesh_quality_type, mesh_quality_measure
        )

        if not cashocs_remesh_flag:
            if current_mesh_quality < mesh_quality_tol_lower:
                raise InputError(
                    "cashocs.geometry.import_mesh",
                    "input_arg",
                    "The quality of the mesh file you have specified is not sufficient for evaluating the cost functional.\n"
                    + f"It currently is {current_mesh_quality:.3e} but has to be at least {mesh_quality_tol_lower:.3e}.",
                )

            if current_mesh_quality < mesh_quality_tol_upper:
                raise InputError(
                    "cashocs.geometry.import_mesh",
                    "input_arg",
                    "The quality of the mesh file you have specified is not sufficient for computing the shape gradient.\n "
                    + f"It currently is {current_mesh_quality:.3e} but has to be at least {mesh_quality_tol_lower:.3e}.",
                )

        else:
            if current_mesh_quality < mesh_quality_tol_lower:
                raise InputError(
                    "cashocs.geometry.import_mesh",
                    "input_arg",
                    "Remeshing failed.\n"
                    "The quality of the mesh file generated through remeshing is not sufficient for evaluating the cost functional.\n"
                    + f"It currently is {current_mesh_quality:.3e} but has to be at least {mesh_quality_tol_lower:.3e}.",
                )

            if current_mesh_quality < mesh_quality_tol_upper:
                raise InputError(
                    "cashocs.geometry.import_mesh",
                    "input_arg",
                    "Remeshing failed.\n"
                    "The quality of the mesh file generated through remeshing is not sufficient for computing the shape gradient.\n "
                    + f"It currently is {current_mesh_quality:.3e} but has to be at least {mesh_quality_tol_upper:.3e}.",
                )

    return mesh, subdomains, boundaries, dx, ds, dS


def regular_mesh(
    n: int = 10,
    L_x: float = 1.0,
    L_y: float = 1.0,
    L_z: Optional[float] = None,
    diagonal: Literal["left", "right", "left/right", "right/left", "crossed"] = "right",
) -> Tuple[
    fenics.Mesh,
    fenics.MeshFunction,
    fenics.MeshFunction,
    fenics.Measure,
    fenics.Measure,
    fenics.Measure,
]:
    r"""Creates a mesh corresponding to a rectangle or cube.

    This function creates a uniform mesh of either a rectangle
    or a cube, starting at the origin and having length specified
    in ``L_x``, ``L_y``, and ``L_z``. The resulting mesh uses ``n`` elements along the
    shortest direction and accordingly many along the longer ones.
    The resulting domain is

    .. math::
        \begin{alignedat}{2}
        &[0, L_x] \times [0, L_y] \quad &&\text{ in } 2D, \\
        &[0, L_x] \times [0, L_y] \times [0, L_z] \quad &&\text{ in } 3D.
        \end{alignedat}

    The boundary markers are ordered as follows:

      - 1 corresponds to :math:`x=0`.

      - 2 corresponds to :math:`x=L_x`.

      - 3 corresponds to :math:`y=0`.

      - 4 corresponds to :math:`y=L_y`.

      - 5 corresponds to :math:`z=0` (only in 3D).

      - 6 corresponds to :math:`z=L_z` (only in 3D).

    Parameters
    ----------
    n : int
        Number of elements in the shortest coordinate direction.
    L_x : float
        Length in x-direction.
    L_y : float
        Length in y-direction.
    L_z : float or None, optional
        Length in z-direction, if this is ``None``, then the geometry
        will be two-dimensional (default is ``None``).
    diagonal : str, optional
        This defines the type of diagonal used to create the box mesh in 2D. This can be
        one of ``"right"``, ``"left"``, ``"left/right"``, ``"right/left"`` or
        ``"crossed"``.

    Returns
    -------
    mesh : fenics.Mesh
        The computational mesh.
    subdomains : fenics.MeshFunction
        A :py:class:`fenics.MeshFunction` object containing the subdomains.
    boundaries : fenics.MeshFunction
        A MeshFunction object containing the boundaries.
    dx : fenics.Measure
        The volume measure of the mesh corresponding to subdomains.
    ds : fenics.Measure
        The surface measure of the mesh corresponding to boundaries.
    dS : fenics.Measure
        The interior facet measure of the mesh corresponding to boundaries.
    """

    if not n > 0:
        raise InputError(
            "cashocs.geometry.regular_mesh", "n", "n needs to be positive."
        )
    if not L_x > 0.0:
        raise InputError(
            "cashocs.geometry.regular_mesh", "L_x", "L_x needs to be positive"
        )
    if not L_y > 0.0:
        raise InputError(
            "cashocs.geometry.regular_mesh", "L_y", "L_y needs to be positive"
        )
    if not (L_z is None or L_z > 0.0):
        raise InputError(
            "cashocs.geometry.regular_mesh",
            "L_z",
            "L_z needs to be positive or None (for 2D mesh)",
        )

    n = int(n)

    if L_z is None:
        sizes = [L_x, L_y]
        dim = 2
    else:
        sizes = [L_x, L_y, L_z]
        dim = 3

    size_min = np.min(sizes)
    num_points = [int(np.round(length / size_min * n)) for length in sizes]

    if L_z is None:
        mesh = fenics.RectangleMesh(
            fenics.Point(0, 0),
            fenics.Point(sizes),
            num_points[0],
            num_points[1],
            diagonal=diagonal,
        )
    else:
        mesh = fenics.BoxMesh(
            fenics.Point(0, 0, 0),
            fenics.Point(sizes),
            num_points[0],
            num_points[1],
            num_points[2],
        )

    subdomains = fenics.MeshFunction("size_t", mesh, dim=dim)
    boundaries = fenics.MeshFunction("size_t", mesh, dim=dim - 1)

    x_min = fenics.CompiledSubDomain(
        "on_boundary && near(x[0], 0, tol)", tol=fenics.DOLFIN_EPS
    )
    x_max = fenics.CompiledSubDomain(
        "on_boundary && near(x[0], length, tol)", tol=fenics.DOLFIN_EPS, length=sizes[0]
    )
    x_min.mark(boundaries, 1)
    x_max.mark(boundaries, 2)

    y_min = fenics.CompiledSubDomain(
        "on_boundary && near(x[1], 0, tol)", tol=fenics.DOLFIN_EPS
    )
    y_max = fenics.CompiledSubDomain(
        "on_boundary && near(x[1], length, tol)", tol=fenics.DOLFIN_EPS, length=sizes[1]
    )
    y_min.mark(boundaries, 3)
    y_max.mark(boundaries, 4)

    if L_z is not None:
        z_min = fenics.CompiledSubDomain(
            "on_boundary && near(x[2], 0, tol)", tol=fenics.DOLFIN_EPS
        )
        z_max = fenics.CompiledSubDomain(
            "on_boundary && near(x[2], length, tol)",
            tol=fenics.DOLFIN_EPS,
            length=sizes[2],
        )
        z_min.mark(boundaries, 5)
        z_max.mark(boundaries, 6)

    dx = _NamedMeasure("dx", mesh, subdomain_data=subdomains)
    ds = _NamedMeasure("ds", mesh, subdomain_data=boundaries)
    dS = _NamedMeasure("dS", mesh)

    return mesh, subdomains, boundaries, dx, ds, dS


def regular_box_mesh(
    n: int = 10,
    S_x: float = 0.0,
    S_y: float = 0.0,
    S_z: Optional[float] = None,
    E_x: float = 1.0,
    E_y: float = 1.0,
    E_z: Optional[float] = None,
    diagonal: Literal["right", "left", "left/right", "right/left", "crossed"] = "right",
) -> Tuple[
    fenics.Mesh,
    fenics.MeshFunction,
    fenics.MeshFunction,
    fenics.Measure,
    fenics.Measure,
    fenics.Measure,
]:
    r"""Creates a mesh corresponding to a rectangle or cube.

    This function creates a uniform mesh of either a rectangle
    or a cube, with specified start (``S_``) and end points (``E_``).
    The resulting mesh uses ``n`` elements along the shortest direction
    and accordingly many along the longer ones. The resulting domain is

    .. math::
        \begin{alignedat}{2}
            &[S_x, E_x] \times [S_y, E_y] \quad &&\text{ in } 2D, \\
            &[S_x, E_x] \times [S_y, E_y] \times [S_z, E_z] \quad &&\text{ in } 3D.
        \end{alignedat}

    The boundary markers are ordered as follows:

      - 1 corresponds to :math:`x=S_x`.

      - 2 corresponds to :math:`x=E_x`.

      - 3 corresponds to :math:`y=S_y`.

      - 4 corresponds to :math:`y=E_y`.

      - 5 corresponds to :math:`z=S_z` (only in 3D).

      - 6 corresponds to :math:`z=E_z` (only in 3D).

    Parameters
    ----------
    n : int
        Number of elements in the shortest coordinate direction.
    S_x : float
        Start of the x-interval.
    S_y : float
        Start of the y-interval.
    S_z : float or None, optional
        Start of the z-interval, mesh is 2D if this is ``None``
        (default is ``None``).
    E_x : float
        End of the x-interval.
    E_y : float
        End of the y-interval.
    E_z : float or None, optional
        End of the z-interval, mesh is 2D if this is ``None``
        (default is ``None``).
    diagonal : str, optional
        This defines the type of diagonal used to create the box mesh in 2D. This can be
        one of ``"right"``, ``"left"``, ``"left/right"``, ``"right/left"`` or
        ``"crossed"``.

    Returns
    -------
    mesh : fenice.Mesh
        The computational mesh.
    subdomains : fenics.MeshFunction
        A MeshFunction object containing the subdomains.
    boundaries : fenics.MeshFunction
        A MeshFunction object containing the boundaries.
    dx : fenics.Measure
        The volume measure of the mesh corresponding to subdomains.
    ds : fenics.Measure
        The surface measure of the mesh corresponding to boundaries.
    dS : fenics.Measure
        The interior facet measure of the mesh corresponding to boundaries.
    """

    n = int(n)

    if not n > 0:
        raise InputError(
            "cashocs.geometry.regular_box_mesh", "n", "This needs to be positive."
        )

    if not S_x < E_x:
        raise InputError(
            "cashocs.geometry.regular_box_mesh",
            "S_x",
            "Incorrect input for the x-coordinate. S_x has to be smaller than E_x.",
        )
    if not S_y < E_y:
        raise InputError(
            "cashocs.geometry.regular_box_mesh",
            "S_y",
            "Incorrect input for the y-coordinate. S_y has to be smaller than E_y.",
        )
    if not ((S_z is None and E_z is None) or (S_z < E_z)):
        raise InputError(
            "cashocs.geometry.regular_box_mesh",
            "S_z",
            "Incorrect input for the z-coordinate. S_z has to be smaller than E_z, or only one of them is specified.",
        )

    if S_z is None:
        lx = E_x - S_x
        ly = E_y - S_y
        sizes = [lx, ly]
        dim = 2
    else:
        lx = E_x - S_x
        ly = E_y - S_y
        lz = E_z - S_z
        sizes = [lx, ly, lz]
        dim = 3

    size_min = np.min(sizes)
    num_points = [int(np.round(length / size_min * n)) for length in sizes]

    if S_z is None:
        mesh = fenics.RectangleMesh(
            fenics.Point(S_x, S_y),
            fenics.Point(E_x, E_y),
            num_points[0],
            num_points[1],
            diagonal=diagonal,
        )
    else:
        mesh = fenics.BoxMesh(
            fenics.Point(S_x, S_y, S_z),
            fenics.Point(E_x, E_y, E_z),
            num_points[0],
            num_points[1],
            num_points[2],
        )

    subdomains = fenics.MeshFunction("size_t", mesh, dim=dim)
    boundaries = fenics.MeshFunction("size_t", mesh, dim=dim - 1)

    x_min = fenics.CompiledSubDomain(
        "on_boundary && near(x[0], sx, tol)", tol=fenics.DOLFIN_EPS, sx=S_x
    )
    x_max = fenics.CompiledSubDomain(
        "on_boundary && near(x[0], ex, tol)", tol=fenics.DOLFIN_EPS, ex=E_x
    )
    x_min.mark(boundaries, 1)
    x_max.mark(boundaries, 2)

    y_min = fenics.CompiledSubDomain(
        "on_boundary && near(x[1], sy, tol)", tol=fenics.DOLFIN_EPS, sy=S_y
    )
    y_max = fenics.CompiledSubDomain(
        "on_boundary && near(x[1], ey, tol)", tol=fenics.DOLFIN_EPS, ey=E_y
    )
    y_min.mark(boundaries, 3)
    y_max.mark(boundaries, 4)

    if S_z is not None:
        z_min = fenics.CompiledSubDomain(
            "on_boundary && near(x[2], sz, tol)", tol=fenics.DOLFIN_EPS, sz=S_z
        )
        z_max = fenics.CompiledSubDomain(
            "on_boundary && near(x[2], ez, tol)", tol=fenics.DOLFIN_EPS, ez=E_z
        )
        z_min.mark(boundaries, 5)
        z_max.mark(boundaries, 6)

    dx = _NamedMeasure("dx", mesh, subdomain_data=subdomains)
    ds = _NamedMeasure("ds", mesh, subdomain_data=boundaries)
    dS = _NamedMeasure("dS", mesh)

    return mesh, subdomains, boundaries, dx, ds, dS
