from __future__ import absolute_import


import firedrake
from firedrake.petsc import PETSc
from firedrake import solving_utils
from . import utils
from . import ufl_utils
import firedrake.variational_solver

__all__ = ["NLVSHierarchy"]


def coarsen_problem(problem):
    u = problem.u
    h, lvl = utils.get_level(u)
    if lvl == -1:
        raise RuntimeError("No hierarchy to coarsen")
    if lvl == 0:
        return None
    new_u = ufl_utils.coarsen_thing(problem.u)
    new_bcs = [ufl_utils.coarsen_thing(bc) for bc in problem.bcs]
    new_J = ufl_utils.coarsen_form(problem.J)
    new_Jp = ufl_utils.coarsen_form(problem.Jp)
    new_F = ufl_utils.coarsen_form(problem.F)

    new_problem = firedrake.NonlinearVariationalProblem(new_F,
                                                        new_u,
                                                        bcs=new_bcs,
                                                        J=new_J,
                                                        Jp=new_Jp,
                                                        form_compiler_parameters=problem.form_compiler_parameters)
    return new_problem


def create_interpolation(dmc, dmf):
    _, clvl = utils.get_level(dmc.getAttr("__fs__")())
    _, flvl = utils.get_level(dmf.getAttr("__fs__")())

    cctx = dmc.getAppCtx()
    fctx = dmf.getAppCtx()

    V_c = dmc.getAttr("__fs__")()
    V_f = dmf.getAttr("__fs__")()

    nrow = sum(x.dof_dset.size * x.dof_dset.cdim for x in V_f)
    ncol = sum(x.dof_dset.size * x.dof_dset.cdim for x in V_c)

    cfn = firedrake.Function(V_c)
    ffn = firedrake.Function(V_f)
    cbcs = cctx._problems[clvl].bcs
    fbcs = fctx._problems[flvl].bcs

    class Interpolation(object):
        def __init__(self, cfn, ffn, cbcs=None, fbcs=None):
            self.cfn = cfn
            self.ffn = ffn
            self.cbcs = cbcs or []
            self.fbcs = fbcs or []

        def mult(self, mat, x, y, inc=False):
            with self.cfn.dat.vec as v:
                x.copy(v)
            firedrake.prolong(self.cfn, self.ffn)
            for bc in self.fbcs:
                bc.zero(self.ffn)
            with self.ffn.dat.vec_ro as v:
                if inc:
                    y.axpy(1.0, v)
                else:
                    v.copy(y)

        def multAdd(self, mat, x, y, w):
            if y.handle == w.handle:
                self.mult(mat, x, w, inc=True)
            else:
                self.mult(mat, x, w)
                w.axpy(1.0, y)

        def multTranspose(self, mat, x, y, inc=False):
            with self.ffn.dat.vec as v:
                x.copy(v)
            firedrake.restrict(self.ffn, self.cfn)
            for bc in self.cbcs:
                bc.zero(self.cfn)
            with self.cfn.dat.vec_ro as v:
                if inc:
                    y.axpy(1.0, v)
                else:
                    v.copy(y)

        def multTransposeAdd(self, mat, x, y, w):
            if y.handle == w.handle:
                self.multTranspose(mat, x, w, inc=True)
            else:
                self.multTranspose(mat, x, w)
                w.axpy(1.0, y)

    ctx = Interpolation(cfn, ffn, cbcs, fbcs)
    mat = PETSc.Mat().create()
    mat.setSizes(((nrow, None), (ncol, None)))
    mat.setType(mat.Type.PYTHON)
    mat.setPythonContext(ctx)
    mat.setUp()
    return mat, None


def create_injection(dmc, dmf):
    _, clvl = utils.get_level(dmc.getAttr("__fs__")())
    _, flvl = utils.get_level(dmf.getAttr("__fs__")())

    cctx = dmc.getAppCtx()

    V_c = dmc.getAttr("__fs__")()
    V_f = dmf.getAttr("__fs__")()

    nrow = sum(x.dof_dset.size * x.dof_dset.cdim for x in V_f)
    ncol = sum(x.dof_dset.size * x.dof_dset.cdim for x in V_c)

    cfn = firedrake.Function(V_c)
    ffn = firedrake.Function(V_f)
    cbcs = cctx._problems[clvl].bcs

    class Injection(object):
        def __init__(self, cfn, ffn, cbcs=None):
            self.cfn = cfn
            self.ffn = ffn
            self.cbcs = cbcs or []

        def multTranspose(self, mat, x, y):
            with self.ffn.dat.vec as v:
                x.copy(v)
            firedrake.inject(self.ffn, self.cfn)
            for bc in self.cbcs:
                bc.apply(self.cfn)
            with self.cfn.dat.vec_ro as v:
                v.copy(y)

    ctx = Injection(cfn, ffn, cbcs)
    mat = PETSc.Mat().create()
    mat.setSizes(((nrow, None), (ncol, None)))
    mat.setType(mat.Type.PYTHON)
    mat.setPythonContext(ctx)
    mat.setUp()
    return mat


class NLVSHierarchy(object):

    _id = 0

    def __init__(self, problem, **kwargs):
        """
        Solve a :class:`NonlinearVariationalProblem` on a hierarchy of meshes.

        :arg problem: A :class:`NonlinearVariationalProblem` to solve.
        :kwarg nullspace: an optional :class:`.VectorSpaceBasis` (or
             :class:`MixedVectorSpaceBasis`) spanning the null space of the
             operator.
        :kwarg solver_parameters: Solver parameters to pass to PETSc.
            This should be a dict mapping PETSc options to values.
            PETSc flag options should be specified with `bool`
            values (:data:`True` for on, :data:`False` for off).

        .. note::

           This solver is set up for use with geometric multigrid,
           that is you can use :data:`"snes_type": "fas"` or
           :data:`"pc_type": "mg"` transparently.
        """
        problems = []
        while True:
            if problem:
                problems.append(problem)
            else:
                break
            problem = coarsen_problem(problem)
        problems.reverse()
        ctx = firedrake.variational_solver._SNESContext(problems)

        dm = firedrake.variational_solver.get_dm(problems[-1])

        parameters, nullspace = firedrake.variational_solver._extract_kwargs(**kwargs)

        snes = PETSc.SNES().create()

        snes.setDM(dm)
        self.problems = problems
        self.snes = snes
        self.ctx = ctx
        self.ctx.set_function(self.snes)
        self.ctx.set_jacobian(self.snes)

        self._opt_prefix = "firedrake_nlvsh_%d_" % NLVSHierarchy._id
        NLVSHierarchy._id += 1
        self.snes.setOptionsPrefix(self._opt_prefix)
        self.parameters = parameters

    @property
    def parameters(self):
        return self._parameters

    @parameters.setter
    def parameters(self, val):
        assert isinstance(val, dict)
        self._parameters = val
        solving_utils.update_parameters(self, self.snes)

    def solve(self):
        dm = self.snes.getDM()

        nlevel = len(self.ctx._problems)
        dm.setAppCtx(self.ctx)
        dm.setCreateMatrix(self.ctx.create_matrix)
        for i in range(nlevel - 1, 0, -1):
            dm = dm.coarsen()
            dm.setAppCtx(self.ctx)

        for i in range(nlevel - 1):
            dm.setCreateInterpolation(create_interpolation)
            dm.setCreateInjection(create_injection)
            dm.setCreateMatrix(self.ctx.create_matrix)
            dm = dm.refine()

        self.snes.setFromOptions()

        for bc in self.problems[-1].bcs:
            bc.apply(self.problems[-1].u)

        with self.problems[-1].u.dat.vec as v:
            self.snes.solve(None, v)

        solving_utils.check_snes_convergence(self.snes)
