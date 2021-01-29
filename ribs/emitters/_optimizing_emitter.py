"""Provides the OptimizingEmitter."""
import numpy as np

from ribs.emitters._emitter_base import EmitterBase
from ribs.emitters.opt import CMAEvolutionStrategy


class OptimizingEmitter(EmitterBase):
    """CMA-ME optimizing emitter.

    This emitter originates in the `CMA-ME paper
    <https://arxiv.org/abs/1912.02400>`_. Initially, it will start at ``x0`` and
    use CMA-ES to optimize for objective values. After CMA-ES converges, the
    emitter will restart the optimizer. It will pick a random elite in the
    archive and begin optimizing from there.

    Args:
        archive (ribs.archives.ArchiveBase): An archive to use when creating and
            inserting solutions. For instance, this can be
            :class:`ribs.archives.GridArchive`.
        x0 (np.ndarray): Initial solution.
        sigma0 (float): Initial step size.
        selection_rule ("mu" or "filter"): Method for selecting solutions in
            CMA-ES. With "mu" selection, the first half of the solutions will be
            selected, while in "filter", any solutions that were added to the
            archive will be selected.
        restart_rule ("no_improvement" or "basic"): Method to use when checking
            for restart. With "basic", only the default CMA-ES convergence rules
            will be used, while with "no_improvement", the emitter will restart
            when none of the proposed solutions were added to the archive.
        weight_rule ("truncation" or "active"): Method for generating weights in
            CMA-ES. Either "truncation" (positive weights only) or "active"
            (include negative weights).
        bounds (None or array-like): Bounds of the solution space. Solutions are
            clipped to these bounds. Pass None to indicate there are no bounds.

            Pass an array-like to specify the bounds for each dim. Each element
            in this array-like can be None to indicate no bound, or a tuple of
            ``(lower_bound, upper_bound)``, where ``lower_bound`` or
            ``upper_bound`` may be None to indicate no bound.
        batch_size (int): Number of solutions to send back in the ask() method.
            If not passed in, a batch size will automatically be calculated.
        seed (int): Value to seed the random number generator. Set to None to
            avoid seeding.
    Raises:
        ValueError: If any of ``selection_rule``, ``restart_rule``, or
            ``weight_rule`` is invalid.
    """

    def __init__(self,
                 archive,
                 x0,
                 sigma0,
                 selection_rule="mu",
                 restart_rule="basic",
                 weight_rule="truncation",
                 bounds=None,
                 batch_size=None,
                 seed=None):
        self._x0 = np.array(x0, dtype=archive.dtype)
        self._sigma0 = sigma0
        EmitterBase.__init__(
            self,
            archive,
            len(self._x0),
            bounds,
            batch_size,
            seed,
        )

        if selection_rule not in ["mu", "filter"]:
            raise ValueError(f"Invalid selection_rule {selection_rule}")
        self._selection_rule = selection_rule

        if restart_rule not in ["basic", "no_improvement"]:
            raise ValueError(f"Invalid restart_rule {restart_rule}")
        self._restart_rule = restart_rule

        opt_seed = None if seed is None else self._rng.integers(10_000)
        self.opt = CMAEvolutionStrategy(sigma0, batch_size, self._solution_dim,
                                        weight_rule, opt_seed,
                                        self._archive.dtype)
        self.opt.reset(self._x0)
        self._num_parents = (self.opt.batch_size //
                             2 if selection_rule == "mu" else None)
        self._batch_size = self.opt.batch_size
        self._restarts = 0  # Currently not exposed publicly.

    @property
    def x0(self):
        """numpy.ndarray: Initial solution for the optimizer."""
        return self._x0

    @property
    def sigma0(self):
        """float: Initial step size for the CMA-ES optimizer."""
        return self._sigma0

    def ask(self):
        """Samples new solutions from a multivariate Gaussian.

        The multivariate Gaussian is parameterized by the CMA-ES optimizer.

        Returns:
            ``(self.batch_size, self.solution_dim)`` array -- contains
            ``batch_size`` new solutions to evaluate.
        """
        return self.opt.ask(self.lower_bounds, self.upper_bounds)

    def _check_restart(self, num_parents):
        """Emitter-side checks for restarting the optimizer.

        The optimizer also has its own checks.
        """
        if self._restart_rule == "no_improvement":
            return num_parents == 0
        return False

    def tell(self, solutions, objective_values, behavior_values):
        """Gives the emitter results from evaluating solutions.

        As solutions are inserted into the archive, we record their objective
        value as well as whether the solution was added to the archive. When
        using "filter" selection, we rank the solutions first by whether they
        were added, and second by the objective value, and when using "mu"
        selection, we rank solely by objective. We then pass the ranked
        solutions to the underlying CMA-ES optimizer to update the search
        parameters.

        Args:
            solutions (numpy.ndarray): Array of solutions generated by this
                emitter's :meth:`ask()` method.
            objective_values (numpy.ndarray): 1D array containing the objective
                function value of each solution.
            behavior_values (numpy.ndarray): ``(n, <behavior space dimension>)``
                array with the behavior space coordinates of each solution.
        """
        # Tuples of (solution was added, objective value, index).
        ranking_data = []
        new_sols = 0
        for i, (sol, obj, beh) in enumerate(
                zip(solutions, objective_values, behavior_values)):
            status, _ = self._archive.add(sol, obj, beh)
            added = bool(status)
            ranking_data.append((added, obj, i))
            if added:
                new_sols += 1

        if self._selection_rule == "filter":
            # Sort by whether the solution was added into the archive, followed
            # by objective value.
            key = lambda x: (x[0], x[1])
        elif self._selection_rule == "mu":
            # Sort only by objective value.
            key = lambda x: x[1]
        ranking_data.sort(reverse=True, key=key)
        indices = [d[2] for d in ranking_data]

        num_parents = (new_sols if self._selection_rule == "filter" else
                       self._num_parents)

        self.opt.tell(solutions[indices], num_parents)

        # Check for reset.
        if (self.opt.check_stop([obj for status, obj, i in ranking_data]) or
                self._check_restart(new_sols)):
            new_x0 = self._archive.get_random_elite()[0]
            self.opt.reset(new_x0)
            self._restarts += 1
