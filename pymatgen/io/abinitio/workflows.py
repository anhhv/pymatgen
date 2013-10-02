"""
Abinit workflows
"""
from __future__ import division, print_function

import sys
import os
import shutil
import time
import abc
import collections
import functools
import numpy as np
import cPickle as pickle

try:
    from pydispatch import dispatcher
except ImportError:
    pass

from pymatgen.core.units import ArrayWithUnit, Ha_to_eV
from pymatgen.core.lattice import Lattice
from pymatgen.core.structure import Structure
from pymatgen.core.design_patterns import Enum, AttrDict
from pymatgen.serializers.json_coders import MSONable, json_pretty_dump
from pymatgen.io.smartio import read_structure
from pymatgen.util.num_utils import iterator_from_slice, chunks, monotonic
from pymatgen.util.string_utils import list_strings, pprint_table, WildCard
from pymatgen.io.abinitio.tasks import (Task, AbinitTask, Dependency, 
                                        Node, ScfTask, NscfTask, HaydockBseTask)
from pymatgen.io.abinitio.strategies import Strategy
from pymatgen.io.abinitio.utils import File, Directory
from pymatgen.io.abinitio.netcdf import ETSF_Reader
from pymatgen.io.abinitio.abiobjects import Smearing, AbiStructure, KSampling, Electrons
from pymatgen.io.abinitio.pseudos import Pseudo
from pymatgen.io.abinitio.strategies import ScfStrategy
from pymatgen.io.abinitio.eos import EOS
from pymatgen.io.abinitio.abitimer import AbinitTimerParser

import logging
logger = logging.getLogger(__name__)

__author__ = "Matteo Giantomassi"
__copyright__ = "Copyright 2013, The Materials Project"
__version__ = "0.1"
__maintainer__ = "Matteo Giantomassi"

__all__ = [
    "Workflow",
    "IterativeWorkflow",
    "BandStructureWorkflow",
#    "RelaxationWorkflow",
#    "DeltaTestWorkflow"
    "GW_Workflow",
    "BSEMDF_Workflow",
    "AbinitFlow",
]


class WorkflowError(Exception):
    """Base class for the exceptions raised by Workflow objects."""


class BaseWorkflow(Node):
    __metaclass__ = abc.ABCMeta

    Error = WorkflowError

    # Basename of the pickle database.
    PICKLE_FNAME = "__workflow__.pickle"

    # interface modeled after subprocess.Popen
    @abc.abstractproperty
    def processes(self):
        """Return a list of objects that support the subprocess.Popen protocol."""

    def poll(self):
        """
        Check if all child processes have terminated. Set and return
        returncode attribute.
        """
        return [task.poll() for task in self]

    def wait(self):
        """
        Wait for child processed to terminate. Set and return returncode attribute.
        """
        return [task.wait() for task in self]

    def communicate(self, input=None):
        """
        Interact with processes: Send data to stdin. Read data from stdout and
        stderr, until end-of-file is reached.
        Wait for process to terminate. The optional input argument should be a
        string to be sent to the child processed, or None, if no data should be
        sent to the children.

        communicate() returns a list of tuples (stdoutdata, stderrdata).
        """
        return [task.communicate(input) for task in self]

    def show_intrawork_deps(self):
        """Show the dependencies within the `Workflow`."""
        table = [["Task #"] + [str(i) for i in range(len(self))]]

        for ii, task1 in enumerate(self):
            line = (1 + len(self)) * [""]
            line[0] = str(ii)
            for jj, task2 in enumerate(self):
                if task1.depends_on(task2):
                    line[jj+1] = "^"

            table.append(line)

        pprint_table(table)

    @property
    def returncodes(self):
        """
        The children return codes, set by poll() and wait() (and indirectly by communicate()).
        A None value indicates that the process hasn't terminated yet.
        A negative value -N indicates that the child was terminated by signal N (Unix only).
        """
        return [task.returncode for task in self]

    @property
    def ncpus_reserved(self):
        """
        Returns the number of CPUs reserved in this moment.
        A CPUS is reserved if it's still not running but 
        we have submitted the task to the queue manager.
        """
        ncpus = 0
        for task in self:
            if task.status == task.S_SUB:
                ncpus += task.tot_ncpus

        return ncpus

    @property
    def ncpus_allocated(self):
        """
        Returns the number of CPUs allocated in this moment.
        A CPU is allocated if it's running a task or if we have
        submitted a task to the queue manager but the job is still pending.
        """
        ncpus = 0
        for task in self:
            if task.status in [task.S_SUB, task.S_RUN]:
                ncpus += task.tot_ncpus
                                                                  
        return ncpus

    @property
    def ncpus_inuse(self):
        """
        Returns the number of CPUs used in this moment.
        A CPU is used if there's a job that is running on it.
        """
        ncpus = 0
        for task in self:
            if task.status == task.S_RUN:
                ncpus += task.tot_ncpus
                                                                  
        return ncpus

    def fetch_task_to_run(self):
        """
        Returns the first task that is ready to run or 
        None if no task can be submitted at present"

        Raises:
            `StopIteration` if all tasks are done.
        """
        #self.check_status()

        # All the tasks are done so raise an exception 
        # that will be handled by the client code.
        if all([task.is_completed for task in self]):
            raise StopIteration("All tasks completed.")

        for task in self:
            if task.can_run:
                #print(task, str(task.status), [task.deps_status])
                return task

        # No task found, this usually happens when we have dependencies. 
        # Beware of possible deadlocks here!
        logger.warning("Possible deadlock in fetch_task_to_run!")
        return None

    @abc.abstractmethod
    def setup(self, *args, **kwargs):
        """Method called before submitting the calculations."""

    def _setup(self, *args, **kwargs):
        self.setup(*args, **kwargs)

    def connect_signals(self):
        """
        Connect the signals within the workflow.
        self is responsible for catching the important signals raised from 
        its task and raise new signals when some particular condition occurs.
        """
        for task in self:
            dispatcher.connect(self.on_ok, signal=task.S_OK, sender=task)

    def on_ok(self, sender):
        """
        This callback is called when one task reaches status S_OK.
        """
        logger.debug("in on_ok with sender %s" % sender)

        all_ok = all(task.status == task.S_OK for task in self)

        if all_ok: 

            if self.finalized:
                return AttrDict(returncode=0, message="Workflow has been already finalized")

            else:
                results = AttrDict(**self.on_all_ok())
                self._finalized = True
                # Signal to possible observers that the `Workflow` reached S_OK
                print("Workflow %s is finalized and broadcasts signal S_OK" % str(self))
                print("Workflow %s status = %s" % (str(self), self.status))
                dispatcher.send(signal=self.S_OK, sender=self)

                self.history.append("Finalized on %s" % time.asctime())

                return results

        return AttrDict(returncode=1, message="Not all tasks are OK!")

    def on_all_ok(self):
        """
        This method is called once the `workflow` is completed i.e. when all the tasks 
        have reached status S_OK. Subclasses should provide their own implementation

        Returns:
            Dictionary that must contain at least the following entries:
                returncode:
                    0 on success. 
                message: 
                    a string that should provide a human-readable description of what has been performed.
        """
        return dict(returncode=0, 
                    message="Calling on_all_ok of the base class!",
                    )

    def get_results(self, *args, **kwargs):
        """
        Method called once the calculations are completed.

        The base version returns a dictionary task_name : TaskResults for each task in self.
        """
        return WorkFlowResults(task_results={task.name: task.results for task in self})

    def build_and_pickle_dump(self, protocol=-1):
        self.build()
        self.pickle_dump(protocol=protocol)

    def pickle_dump(self, protocol=-1):
        """Save the status of the object in pickle format."""
        filepath = os.path.join(self.workdir, self.PICKLE_FNAME)

        # TODO atomic transaction.
        with open(filepath, mode="w" if protocol == 0 else "wb" ) as fh:
            pickle.dump(self, fh, protocol=protocol)
    

class Workflow(BaseWorkflow):
    """
    A Workflow is a list of (possibly connected) tasks.
    """
    Error = WorkflowError

    def __init__(self, workdir=None, manager=None):
        """
        Args:
            workdir:
                Path to the working directory.
            manager:
                `TaskManager` object.
        """
        super(Workflow, self).__init__()

        self._tasks = []

        if workdir is not None:
            self.set_workdir(workdir)

        if manager is not None:
            self.set_manager(manager)

    @property
    def id(self):
        """Task identifier."""
        return self._id

    @property
    def finalized(self):
        """True if the `Workflow` has been finalized."""
        try:
            return self._finalized

        except AttributeError:
            return False

    def set_manager(self, manager):
        """Set the `TaskManager` to use to launch the Task."""
        self.manager = manager.deepcopy()
        for task in self:
            task.set_manager(manager)

    def set_workdir(self, workdir):
        """Set the working directory. Cannot be set more than once."""

        if hasattr(self, "workdir") and self.workdir != workdir:
            raise ValueError("self.workdir != workdir: %s, %s" % (self.workdir,  workdir))

        self.workdir = os.path.abspath(workdir)
                                                                       
        # Directories with (input|output|temporary) data.
        # The workflow will use these directories to connect 
        # itself to other workflows and/or to produce new data 
        # that will be used by its children.
        self.indir = Directory(os.path.join(self.workdir, "indata"))
        self.outdir = Directory(os.path.join(self.workdir, "outdata"))
        self.tmpdir = Directory(os.path.join(self.workdir, "tmpdata"))

    def __len__(self):
        return len(self._tasks)

    def __iter__(self):
        return self._tasks.__iter__()

    def __getitem__(self, slice):
        return self._tasks[slice]

    def chunks(self, chunk_size):
        """Yield successive chunks of tasks of lenght chunk_size."""
        for tasks in chunks(self, chunk_size):
            yield tasks

    def opath_from_ext(self, ext):
        """
        Returns the path of the output file with extension ext.
        Use it when the file does not exist yet.
        """
        return self.indir.path_in("in_" + ext)

    def opath_from_ext(self, ext):
        """
        Returns the path of the output file with extension ext.
        Use it when the file does not exist yet.
        """
        return self.outdir.path_in("out_" + ext)

    @property
    def processes(self):
        return [task.process for task in self]

    @property
    def all_done(self):
        """True if all the `Task` in the `Workflow` are done."""
        return all([task.status >= task.S_DONE for task in self])

    @property
    def isnc(self):
        """True if norm-conserving calculation."""
        return all(task.isnc for task in self)

    @property
    def ispaw(self):
        """True if PAW calculation."""
        return all(task.ispaw for task in self)

    #@property
    #def status(self):
    #    """
    #    The status of the `Workflow` equal to the most 
    #    restricting status of the tasks.
    #    """
    #    return min(task.status for task in self)

    def status_counter(self):
        """
        Returns a `Counter` object that counts the number of task with 
        given status (use the string representation of the status as key).
        """
        counter = collections.Counter() 

        for task in self:
            counter[str(task.status)] += 1

        return counter

    def allocate(self):
        for i, task in enumerate(self):

            if not hasattr(task, "manager"):
                task.set_manager(self.manager)

            task_workdir = os.path.join(self.workdir, "task_" + str(i))

            if not hasattr(task, "workdir"):
                task.set_workdir(task_workdir)
            else:
                if task.workdir != task_workdir:
                    raise ValueError("task.workdir != task_workdir: %s, %s" % (task.workdir, task_workdir))


    def register(self, obj, deps=None, manager=None, task_class=None):
        """
        Registers a new `Task` and add it to the internal list, taking into account possible dependencies.

        Args:
            obj:
                `Strategy` object or `AbinitInput` instance.
                if Strategy object, we create a new `AbinitTask` from the input strategy and add it to the list.
            deps:
                Dictionary specifying the dependency of this node.
                None means that this obj has no dependency.
            manager:
                The `TaskManager` responsible for the submission of the task. If manager is None, we use 
                the `TaskManager` specified during the creation of the `Workflow`.
            task_class:
                `AbinitTask` subclass to instanciate. 
                 Mainly used if the workflow is not able to find it automatically.

        Returns:   
            `Task` object
        """
        task_workdir = None
        if hasattr(self, "workdir"):
            task_workdir = os.path.join(self.workdir, "task_" + str(len(self)))

        # Set the class
        if task_class is None:
            task_class = AbinitTask

        if isinstance(obj, Strategy):
            # Create the new task (note the factory so that we create subclasses easily).
            task = task_class(obj, task_workdir, manager)

        else:
            # Create the new task from the input. Note that no subclasses are instanciated here.
            task = task_class.from_input(obj, task_workdir, manager)

        self._tasks.append(task)

        # Handle possible dependencies.
        if deps is not None:
            deps = [Dependency(node, exts) for (node, exts) in deps.items()]
            task.add_deps(deps)

        return task

    def path_in_workdir(self, filename):
        """Create the absolute path of filename in the working directory."""
        return os.path.join(self.workdir, filename)

    def setup(self, *args, **kwargs):
        """
        Method called before running the calculations.
        The default implementation is empty.
        """

    def build(self, *args, **kwargs):
        """Creates the top level directory."""
        # Create the directories of the workflow.
        self.indir.makedirs()
        self.outdir.makedirs()
        self.tmpdir.makedirs()

        # Build dirs and files of each task.
        for task in self:
            task.build(*args, **kwargs)

        # Connect signals within the workflow.
        self.connect_signals()

    @property
    def status(self):
        """
        Returns the status of the workflow i.e. the minimum of the status of the tasks.
        """
        return self.get_all_status(only_min=True)

    #def set_status(self, status):

    def get_all_status(self, only_min=False):
        """
        Returns a list with the status of the tasks in self.

        Args:
            only_min:
                If True, the minimum of the status is returned.
        """
        if len(self) == 0:
            # The workflow will be created in the future.
            if only_min:
                return Task.S_INIT
            else:
                return [Task.S_INIT]

        self.check_status()

        status_list = [task.status for task in self]
        #print("status_list", status_list)

        if only_min:
            return min(status_list)
        else:
            return status_list

    def check_status(self):
        """Check the status of the tasks."""
        # Recompute the status of the tasks
        for task in self:
            task.check_status()

        # Take into account possible dependencies.
        for task in self:
            if task.status <= task.S_SUB and all([status == task.S_OK for status in task.deps_status]): 
                task.set_status(task.S_READY)

    def rmtree(self, exclude_wildcard=""):
        """
        Remove all files and directories in the working directory

        Args:
            exclude_wildcard:
                Optional string with regular expressions separated by |.
                Files matching one of the regular expressions will be preserved.
                example: exclude_wildard="*.nc|*.txt" preserves all the files
                whose extension is in ["nc", "txt"].

        """
        if not exclude_wildcard:
            shutil.rmtree(self.workdir)

        else:
            w = WildCard(exclude_wildcard)

            for dirpath, dirnames, filenames in os.walk(self.workdir):
                for fname in filenames:
                    path = os.path.join(dirpath, fname)
                    if not w.match(fname):
                        os.remove(path)

    def rm_indatadir(self):
        """Remove all the indata directories."""
        for task in self:
            task.rm_indatadir()

    def rm_outdatadir(self):
        """Remove all the indata directories."""
        for task in self:
            task.rm_outatadir()

    def rm_tmpdatadir(self):
        """Remove all the tmpdata directories."""
        for task in self:
            task.rm_tmpdatadir()

    def move(self, dest, isabspath=False):
        """
        Recursively move self.workdir to another location. This is similar to the Unix "mv" command.
        The destination path must not already exist. If the destination already exists
        but is not a directory, it may be overwritten depending on os.rename() semantics.

        Be default, dest is located in the parent directory of self.workdir, use isabspath=True
        to specify an absolute path.
        """
        if not isabspath:
            dest = os.path.join(os.path.dirname(self.workdir), dest)

        shutil.move(self.workdir, dest)

    def submit_tasks(self, *args, **kwargs):
        """
        Submits the task in self.
        """
        for task in self:
            task.start(*args, **kwargs)
            # FIXME
            task.wait()

    def start(self, *args, **kwargs):
        """
        Start the work. Calls build and _setup first, then the tasks are submitted.
        Non-blocking call
        """
        # Build dirs and files.
        self.build(*args, **kwargs)

        # Initial setup
        self._setup(*args, **kwargs)

        # Submit tasks (does not block)
        self.submit_tasks(*args, **kwargs)

    def read_etotal(self):
        """
        Reads the total energy from the GSR file produced by the task.

        Return a numpy array with the total energies in Hartree
        The array element is set to np.inf if an exception is raised while reading the GSR file.
        """
        if not self.all_done:
            raise self.Error("Some task is still in running/submitted state")

        etotal = []
        for task in self:
            # Open the GSR file and read etotal (Hartree)
            gsr_path = task.outdir.has_abiext("GSR")
            etot = np.inf
            if gsr_path:
                with ETSF_Reader(gsr_path) as r:
                    etot = r.read_value("etotal")
                
            etotal.append(etot)

        return etotal

    def json_dump(self, filename):
        json_pretty_dump(self.to_dict, filename)
                                                  
    @classmethod
    def json_load(cls, filename):
        return cls.from_dict(json_load(filename))

    def parse_timers(self):
        """Parse the TIMER section reported in the ABINIT output files."""

        filenames = [task.output_file.path for task in self]
                                                                           
        parser = AbinitTimerParser()
        parser.parse(filenames)
                                                                           
        return parser


class IterativeWorkflow(Workflow):
    """
    This object defines a `Workflow` that produces `Tasks` until a particular 
    condition is satisfied (mainly used for convergence studies or iterative algorithms.)
    """
    __metaclass__ = abc.ABCMeta

    def __init__(self, strategy_generator, max_niter=25, workdir=None, manager=None):
        """
        Args:
            strategy_generator:
                Generator object that produces `Strategy` objects.
            max_niter:
                Maximum number of iterations. A negative value or zero value
                is equivalent to having an infinite number of iterations.
            workdir:
                Working directory.
            manager:
                `TaskManager` class.
        """
        super(IterativeWorkflow, self).__init__(workdir, manager)

        self.strategy_generator = strategy_generator

        self.max_niter = max_niter
        self.niter = 0

    def next_task(self):
        """
        Generate and register a new `Task`.

        Returns: 
            New `Task` object
        """
        try:
            next_strategy = next(self.strategy_generator)

        except StopIteration:
            raise

        self.register(next_strategy)
        assert len(self) == self.niter

        return self[-1]

    def submit_tasks(self, *args, **kwargs):
        """
        Run the tasks till self.exit_iteration says to exit 
        or the number of iterations exceeds self.max_niter

        Returns: 
            dictionary with the final results
        """
        self.niter = 1

        while True:
            if self.niter > self.max_niter > 0:
                logger.debug("niter %d > max_niter %d" % (self.niter, self.max_niter))
                break

            try:
                task = self.next_task()
            except StopIteration:
                break

            # Start the task and block till completion.
            task.start(*args, **kwargs)
            task.wait()

            data = self.exit_iteration(*args, **kwargs)

            if data["exit"]:
                break

            self.niter += 1

    @abc.abstractmethod
    def exit_iteration(self, *args, **kwargs):
        """
        Return a dictionary with the results produced at the given iteration.
        The dictionary must contains an entry "converged" that evaluates to
        True if the iteration should be stopped.
        """


def check_conv(values, tol, min_numpts=1, mode="abs", vinf=None):
    """
    Given a list of values and a tolerance tol, returns the leftmost index for which

        abs(value[i] - vinf) < tol if mode == "abs"

    or

        abs(value[i] - vinf) / vinf < tol if mode == "rel"

    returns -1 if convergence is not achieved. By default, vinf = values[-1]

    Args:
        tol:
            Tolerance
        min_numpts:
            Minimum number of points that must be converged.
        mode:
            "abs" for absolute convergence, "rel" for relative convergence.
        vinf:
            Used to specify an alternative value instead of values[-1].
    """
    vinf = values[-1] if vinf is None else vinf

    if mode == "abs":
        vdiff = [abs(v - vinf) for v in values]
    elif mode == "rel":
        vdiff = [abs(v - vinf) / vinf for v in values]
    else:
        raise ValueError("Wrong mode %s" % mode)

    numpts = len(vdiff)
    i = -2

    if (numpts > min_numpts) and vdiff[-2] < tol:
        for i in range(numpts-1, -1, -1):
            if vdiff[i] > tol:
                break
        if (numpts - i -1) < min_numpts: i = -2

    return i + 1


def compute_hints(ecut_list, etotal, atols_mev, pseudo, min_numpts=1, stream=sys.stdout):
    de_low, de_normal, de_high = [a / (1000 * Ha_to_eV) for a in atols_mev]

    num_ene = len(etotal)
    etotal_inf = etotal[-1]

    ihigh   = check_conv(etotal, de_high, min_numpts=min_numpts)
    inormal = check_conv(etotal, de_normal)
    ilow    = check_conv(etotal, de_low)

    accidx = {"H": ihigh, "N": inormal, "L": ilow}

    table = []; app = table.append

    app(["iter", "ecut", "etotal", "et-e_inf [meV]", "accuracy",])
    for idx, (ec, et) in enumerate(zip(ecut_list, etotal)):
        line = "%d %.1f %.7f %.3f" % (idx, ec, et, (et-etotal_inf) * Ha_to_eV * 1.e+3)
        row = line.split() + ["".join(c for c,v in accidx.items() if v == idx)]
        app(row)

    if stream is not None:
        stream.write("pseudo: %s\n" % pseudo.name)
        pprint_table(table, out=stream)

    ecut_high, ecut_normal, ecut_low = 3 * (None,)
    exit = (ihigh != -1)

    if exit:
        ecut_low    = ecut_list[ilow]
        ecut_normal = ecut_list[inormal]
        ecut_high   = ecut_list[ihigh]

    aug_ratios = [1,]
    aug_ratio_low, aug_ratio_normal, aug_ratio_high = 3 * (1,)

    data = {
        "exit"       : ihigh != -1,
        "etotal"     : list(etotal),
        "ecut_list"  : ecut_list,
        "aug_ratios" : aug_ratios,
        "low"        : {"ecut": ecut_low, "aug_ratio": aug_ratio_low},
        "normal"     : {"ecut": ecut_normal, "aug_ratio": aug_ratio_normal},
        "high"       : {"ecut": ecut_high, "aug_ratio": aug_ratio_high},
        "pseudo_name": pseudo.name,
        "pseudo_path": pseudo.path,
        "atols_mev"  : atols_mev,
        "dojo_level" : 0,
    }

    return data


def plot_etotal(ecut_list, etotals, aug_ratios, **kwargs):
    """
    Uses Matplotlib to plot the energy curve as function of ecut

    Args:
        ecut_list:
            List of cutoff energies
        etotals:
            Total energies in Hartree, see aug_ratios
        aug_ratios:
            List augmentation rations. [1,] for norm-conserving, [4, ...] for PAW
            The number of elements in aug_ration must equal the number of (sub)lists
            in etotals. Example:

                - NC: etotals = [3.4, 4,5 ...], aug_ratios = [1,]
                - PAW: etotals = [[3.4, ...], [3.6, ...]], aug_ratios = [4,6]

        =========     ==============================================================
        kwargs        description
        =========     ==============================================================
        show          True to show the figure
        savefig       'abc.png' or 'abc.eps'* to save the figure to a file.
        =========     ==============================================================

    Returns:
        `matplotlib` figure.
    """
    show = kwargs.pop("show", True)
    savefig = kwargs.pop("savefig", None)

    import matplotlib.pyplot as plt
    fig = plt.figure()
    ax = fig.add_subplot(1,1,1)

    npts = len(ecut_list)

    if len(aug_ratios) != 1 and len(aug_ratios) != len(etotals):
        raise ValueError("The number of sublists in etotal must equal the number of aug_ratios")

    if len(aug_ratios) == 1:
        etotals = [etotals,]

    lines, legends = [], []

    emax = -np.inf
    for (aratio, etot) in zip(aug_ratios, etotals):
        emev = np.array(etot) * Ha_to_eV * 1000
        emev_inf = npts * [emev[-1]]
        yy = emev - emev_inf

        emax = np.max(emax, np.max(yy))

        line, = ax.plot(ecut_list, yy, "-->", linewidth=3.0, markersize=10)

        lines.append(line)
        legends.append("aug_ratio = %s" % aratio)

    ax.legend(lines, legends, 'upper right', shadow=True)

    # Set xticks and labels.
    ax.grid(True)
    ax.set_xlabel("Ecut [Ha]")
    ax.set_ylabel("$\Delta$ Etotal [meV]")
    ax.set_xticks(ecut_list)

    #ax.yaxis.set_view_interval(-10, emax + 0.01 * abs(emax))
    #ax.xaxis.set_view_interval(-10, 20)
    ax.yaxis.set_view_interval(-10, 20)

    ax.set_title("$\Delta$ Etotal Vs Ecut")

    if show:
        plt.show()

    if savefig is not None:
        fig.savefig(savefig)

    return fig


class PseudoConvergence(Workflow):

    def __init__(self, workdir, manager, pseudo, ecut_list, atols_mev,
                 toldfe=1.e-8, spin_mode="polarized", 
                 acell=(8, 9, 10), smearing="fermi_dirac:0.1 eV"):

        super(PseudoConvergence, self).__init__(workdir, manager)

        # Temporary object used to build the strategy.
        generator = PseudoIterativeConvergence(workdir, manager, pseudo, ecut_list, atols_mev,
                                               toldfe    = toldfe,
                                               spin_mode = spin_mode,
                                               acell     = acell,
                                               smearing  = smearing,
                                               max_niter = len(ecut_list),
                                              )
        self.atols_mev = atols_mev
        self.pseudo = Pseudo.aspseudo(pseudo)

        self.ecut_list = []
        for ecut in ecut_list:
            strategy = generator.strategy_with_ecut(ecut)
            self.ecut_list.append(ecut)
            self.register(strategy)

    def get_results(self, *args, **kwargs):

        # Get the results of the tasks.
        wf_results = super(PseudoConvergence, self).get_results()

        etotal = self.read_etotal()
        data = compute_hints(self.ecut_list, etotal, self.atols_mev, self.pseudo)

        plot_etotal(data["ecut_list"], data["etotal"], data["aug_ratios"],
            show=False, savefig=self.path_in_workdir("etotal.pdf"))

        wf_results.update(data)

        if not monotonic(etotal, mode="<", atol=1.0e-5):
            logger.warning("E(ecut) is not decreasing")
            wf_results.push_exceptions("E(ecut) is not decreasing:\n" + str(etotal))

        #if kwargs.get("json_dump", True):
        #    wf_results.json_dump(self.path_in_workdir("results.json"))

        return wf_results


class PseudoIterativeConvergence(IterativeWorkflow):

    def __init__(self, workdir, manager, pseudo, ecut_list_or_slice, atols_mev,
                 toldfe=1.e-8, spin_mode="polarized", 
                 acell=(8, 9, 10), smearing="fermi_dirac:0.1 eV", max_niter=50,):
        """
        Args:
            workdir:
                Working directory.
            pseudo:
                string or Pseudo instance
            ecut_list_or_slice:
                List of cutoff energies or slice object (mainly used for infinite iterations).
            atols_mev:
                List of absolute tolerances in meV (3 entries corresponding to accuracy ["low", "normal", "high"]
            manager:
                `TaskManager` object.
            spin_mode:
                Defined how the electronic spin will be treated.
            acell:
                Lengths of the periodic box in Bohr.
            smearing:
                Smearing instance or string in the form "mode:tsmear". Default: FemiDirac with T=0.1 eV
        """
        self.pseudo = Pseudo.aspseudo(pseudo)

        self.atols_mev = atols_mev
        self.toldfe = toldfe
        self.spin_mode = spin_mode
        self.smearing = Smearing.assmearing(smearing)
        self.acell = acell

        if isinstance(ecut_list_or_slice, slice):
            self.ecut_iterator = iterator_from_slice(ecut_list_or_slice)
        else:
            self.ecut_iterator = iter(ecut_list_or_slice)

        # Construct a generator that returns strategy objects.
        def strategy_generator():
            for ecut in self.ecut_iterator:
                yield self.strategy_with_ecut(ecut)

        super(PseudoIterativeConvergence, self).__init__(strategy_generator(), 
              max_niter=max_niter, workdir=workdir, manager=manager, )

        if not self.isnc:
            raise NotImplementedError("PAW convergence tests are not supported yet")

    def strategy_with_ecut(self, ecut):
        """Return a Strategy instance with given cutoff energy ecut."""

        # Define the system: one atom in a box of lenghts acell.
        boxed_atom = AbiStructure.boxed_atom(self.pseudo, acell=self.acell)

        # Gamma-only sampling.
        gamma_only = KSampling.gamma_only()

        # Setup electrons.
        electrons = Electrons(spin_mode=self.spin_mode, smearing=self.smearing)

        # Don't write WFK files.
        extra_abivars = {
            "ecut" : ecut,
            "prtwf": 0,
            "toldfe": self.toldfe,
        }
        strategy = ScfStrategy(boxed_atom, self.pseudo, gamma_only,
                               spin_mode=self.spin_mode, smearing=self.smearing,
                               charge=0.0, scf_algorithm=None,
                               use_symmetries=True, **extra_abivars)

        return strategy

    @property
    def ecut_list(self):
        """The list of cutoff energies computed so far"""
        return [float(task.strategy.ecut) for task in self]

    def check_etotal_convergence(self, *args, **kwargs):
        return compute_hints(self.ecut_list, self.read_etotal(), self.atols_mev,
                             self.pseudo)

    def exit_iteration(self, *args, **kwargs):
        return self.check_etotal_convergence(self, *args, **kwargs)

    def get_results(self, *args, **kwargs):
        """Return the results of the tasks."""
        wf_results = super(PseudoIterativeConvergence, self).get_results()

        data = self.check_etotal_convergence()

        ecut_list, etotal, aug_ratios = data["ecut_list"],  data["etotal"], data["aug_ratios"]

        plot_etotal(ecut_list, etotal, aug_ratios,
            show=False, savefig=self.path_in_workdir("etotal.pdf"))

        wf_results.update(data)

        if not monotonic(data["etotal"], mode="<", atol=1.0e-5):
            logger.warning("E(ecut) is not decreasing")
            wf_results.push_exceptions("E(ecut) is not decreasing\n" + str(etotal))

        #if kwargs.get("json_dump", True):
        #    wf_results.json_dump(self.path_in_workdir("results.json"))

        return wf_results


class BandStructureWorkflow(Workflow):
    """Workflow for band structure calculations."""
    def __init__(self, scf_input, nscf_input, dos_input=None, workdir=None, manager=None):
        """
        Args:
            scf_input:
                Input for the SCF run or `SCFStrategy` object.
            nscf_input:
                Input for the NSCF run or `NSCFStrategy` object defining the band structure calculation.
            dos_input:
                Input for the DOS run or `NSCFStrategy` object
                DOS is computed only if dos_input is not None.
            workdir:
                Working directory.
            manager:
                `TaskManager` object.
        """
        super(BandStructureWorkflow, self).__init__(workdir=workdir, manager=manager)

        # Register the GS-SCF run.
        self.scf_task = self.register(scf_input, task_class=ScfTask)

        # Register the NSCF run and its dependency
        self.nscf_task = self.register(nscf_input, deps={self.scf_task: "DEN"}, task_class=NscfTask)

        # Add DOS computation
        self.dos_task = None

        if dos_input is not None:
            self.dos_task = self.register(dos_input, deps={self.scf_task: "DEN"}, task_class=NscfTask)


class RelaxationWorkflow(Workflow):

    def __init__(self, relax_input, workdir=None, manager=None):
        """
        Args:
            workdir:
                Working directory.
            manager:
                `TaskManager` object.
            relax_input:
                Input for the relaxation run or `RelaxStrategy` object.
        """
        super(Relaxation, self).__init__(workdir=workdir, manager=manager)

        task = self.register(relax_input)


class DeltaTestWorkflow(Workflow):

    def __init__(self, workdir, manager, structure_or_cif, pseudos, kppa,
                 spin_mode="polarized", toldfe=1.e-8, smearing="fermi_dirac:0.1 eV",
                 accuracy="normal", ecut=None, ecutsm=0.05, chksymbreak=0): # FIXME Hack

        super(DeltaTest, self).__init__(workdir, manager)

        if isinstance(structure_or_cif, Structure):
            structure = structure_or_cif
        else:
            # Assume CIF file
            structure = read_structure(structure_or_cif)

        structure = AbiStructure.asabistructure(structure)

        smearing = Smearing.assmearing(smearing)

        self._input_structure = structure

        v0 = structure.volume

        # From 94% to 106% of the equilibrium volume.
        self.volumes = v0 * np.arange(94, 108, 2) / 100.

        for vol in self.volumes:

            new_lattice = structure.lattice.scale(vol)

            new_structure = Structure(new_lattice, structure.species, structure.frac_coords)
            new_structure = AbiStructure.asabistructure(new_structure)

            extra_abivars = {
                "ecutsm": ecutsm,
                "toldfe": toldfe,
                "prtwf" : 0,
                "paral_kgb": 0,
            }

            if ecut is not None:
                extra_abivars.update({"ecut": ecut})

            ksampling = KSampling.automatic_density(new_structure, kppa,
                                                    chksymbreak=chksymbreak)

            scf_input = ScfStrategy(new_structure, pseudos, ksampling,
                                    accuracy=accuracy, spin_mode=spin_mode,
                                    smearing=smearing, **extra_abivars)

            self.register(scf_input, task_class=ScfTask)

    def get_results(self, *args, **kwargs):
        num_sites = self._input_structure.num_sites

        etotal = ArrayWithUnit(self.read_etotal(), "Ha").to("eV")

        wf_results = super(DeltaTest, self).get_results()

        wf_results.update({
            "etotal"    : list(etotal),
            "volumes"   : list(self.volumes),
            "natom"     : num_sites,
            "dojo_level": 1,
        })


        try:
            #eos_fit = EOS.Murnaghan().fit(self.volumes/num_sites, etotal/num_sites)
            #print("murn",eos_fit)
            #eos_fit.plot(show=False, savefig=self.path_in_workdir("murn_eos.pdf"))

            # Use same fit as the one employed for the deltafactor.
            eos_fit = EOS.DeltaFactor().fit(self.volumes/num_sites, etotal/num_sites)
            #print("delta",eos_fit)
            eos_fit.plot(show=False, savefig=self.path_in_workdir("eos.pdf"))

            wf_results.update({
                "v0": eos_fit.v0,
                "b0": eos_fit.b0,
                "b0_GPa": eos_fit.b0_GPa,
                "b1": eos_fit.b1,
            })

        except EOS.Error as exc:
            wf_results.push_exceptions(exc)

        if kwargs.get("json_dump", True):
            wf_results.json_dump(self.path_in_workdir("results.json"))

        # Write data for the computation of the delta factor
        with open(self.path_in_workdir("deltadata.txt"), "w") as fh:
            fh.write("# Volume/natom [Ang^3] Etotal/natom [eV]\n")
            for (v, e) in zip(self.volumes, etotal):
                fh.write("%s %s\n" % (v/num_sites, e/num_sites))

        return wf_results


class GW_Workflow(Workflow):

    def __init__(self, scf_input, nscf_input, scr_input, sigma_strategy,
                workdir=None, manager=None):
        """
        Workflow for GW calculations.

        Args:
            scf_input:
                Input for the SCF run or `SCFStrategy` object.
            nscf_input:
                Input for the NSCF run or `NSCFStrategy` object.
            scr_input:
                Input for the screening run or `ScrStrategy` object 
            sigma_strategy:
                Strategy for the self-energy run.
            workdir:
                Working directory of the calculation.
            manager:
                `TaskManager` object.
        """
        super(GW_Workflow, self).__init__(workdir=workdir, manager=manager)

        # Register the GS-SCF run.
        self.scf_task = self.register(scf_input, task_class=ScfTask)

        # Construct the input for the NSCF run.
        self.nscf_task = self.register(nscf_input, deps={self.scf_task: "DEN"}, task_class=NscfTask)

        # Register the SCREENING run.
        self.scr_task = self.register(scr_input, deps={self.nscf_task: "WFK"})

        # Register the SIGMA run.
        self.sigma_task = self.register(sigma_strategy, deps={self.nscf_task: "WFK", self.scr_task: "SCR"})


class BSEMDF_Workflow(Workflow):

    def __init__(self, scf_input, nscf_input, bse_input, workdir=None, manager=None):
        """
        Workflow for simple BSE calculations in which the self-energy corrections 
        are approximated by the scissors operator and the screening in modeled 
        with the model dielectric function.

        Args:
            scf_input:
                Input for the SCF run or `ScfStrategy` object.
            nscf_input:
                Input for the NSCF run or `NscfStrategy` object.
            bse_input:
                Input for the BSE run or `BSEStrategy` object.
            workdir:
                Working directory of the calculation.
            manager:
                `TaskManager`.
        """
        super(BSEMDF_Workflow, self).__init__(workdir=workdir, manager=manager)

        # Register the GS-SCF run.
        self.scf_task = self.register(scf_input, task_class=ScfTask)

        # Construct the input for the NSCF run.
        self.nscf_task = self.register(nscf_input, deps={self.scf_task: "DEN"}, task_class=NscfTask)

        # Construct the input for the BSE run.
        self.bse_task = self.register(bse_input, deps={self.nscf_task: "WFK"}, task_class=HaydockBseTask)


class WorkflowResults(dict, MSONable):
    """
    Dictionary used to store some of the results produce by a Task object
    """
    _MANDATORY_KEYS = [
        "task_results",
    ]

    _EXC_KEY = "_exceptions"

    def __init__(self, *args, **kwargs):
        super(WorkflowResults, self).__init__(*args, **kwargs)

        if self._EXC_KEY not in self:
            self[self._EXC_KEY] = []

    @property
    def exceptions(self):
        return self[self._EXC_KEY]

    def push_exceptions(self, *exceptions):
        for exc in exceptions:
            newstr = str(exc)
            if newstr not in self.exceptions:
                self[self._EXC_KEY] += [newstr]

    def assert_valid(self):
        """
        Returns empty string if results seem valid.

        The try assert except trick allows one to get a string with info on the exception.
        We use the += operator so that sub-classes can add their own message.
        """
        # Validate tasks.
        for tres in self.task_results:
            self[self._EXC_KEY] += tres.assert_valid()

        return self[self._EXC_KEY]

    @property
    def to_dict(self):
        d = {k: v for k,v in self.items()}
        d["@module"] = self.__class__.__module__
        d["@class"] = self.__class__.__name__
        return d

    @classmethod
    def from_dict(cls, d):
        mydict = {k: v for k,v in d.items() if k not in ["@module", "@class",]}
        return cls(mydict)


class AbinitFlow(collections.Iterable):
    """
    This object is a container of workflows. Its main task is managing the 
    possible inter-depencies among the workflows and the generation of
    dynamic worflows that are generates by callabacks registered by the user.
    """
    #PICKLE_FNAME = "__AbinitFlow__.pickle"
    PICKLE_FNAME = "__workflow__.pickle"

    def __init__(self, workdir, manager):
        """
        Args:
            workdir:
                String specifying the directory where the workflows will be produced.
            manager:
                `TaskManager` object responsible for the submission of the jobs.
        """
        self.workdir = os.path.abspath(workdir)

        self.manager = manager.deepcopy()

        # List of workflows.
        self._works = []

        # List of callbacks that must be executed when the dependencies reach S_OK
        self._callbacks = []

    def __repr__(self):
        return "<%s at %s, workdir=%s>" % (self.__class__.__name__, id(self), self.workdir)

    def __str__(self):
        return "<%s, workdir=%s>" % (self.__class__.__name__, self.workdir)

    def __len__(self):
        return len(self.works)

    def __iter__(self):
        return self.works.__iter__()

    def __getitem__(self, slice):
        return self.works[slice]

    @property
    def works(self):
        """List of `Workflow` objects."""
        return self._works

    @property
    def ncpus_reserved(self):
        """
        Returns the number of CPUs reserved in this moment.
        A CPUS is reserved if it's still not running but 
        we have submitted the task to the queue manager.
        """
        return sum(work.ncpus_reverved for work in self)

    @property
    def ncpus_allocated(self):
        """
        Returns the number of CPUs allocated in this moment.
        A CPU is allocated if it's running a task or if we have
        submitted a task to the queue manager but the job is still pending.
        """
        return sum(work.ncpus_allocated for work in self)

    @property
    def ncpus_inuse(self):
        """
        Returns the number of CPUs used in this moment.
        A CPU is used if there's a job that is running on it.
        """
        return sum(work.ncpus_inuse for work in self)

    def used_ids(self):
        """
        Returns a set with all the ids used so far to identify `Task` and `Workflow`.
        """
        ids = []
        for work in self:
            ids.append(work.node_id)
            for task in work:
                ids.append(task.node_id)

        used_ids = set(ids)
        assert len(ids_set) == len(ids)

        return used_ids

    def generate_new_nodeid(self):
        """Returns an unused node identifier."""
        used_ids = self.used_ids()

        import itertools
        for nid in intertools.count():
            if nid not in used_ids:
                return nid

    def check_status(self):
        """Check the status of the workflows in self."""
        for work in self:
            work.check_status()

    def build(self, *args, **kwargs):
        for work in self:
            work.build(*args, **kwargs)

    def build_and_pickle_dump(self, protocol=-1):
        self.build()
        self.pickle_dump(protocol=protocol)

    def pickle_dump(self, protocol=-1):
        """Save the status of the object in pickle format."""
        filepath = os.path.join(self.workdir, self.PICKLE_FNAME)

        with open(filepath, mode="w" if protocol == 0 else "wb") as fh:
            pickle.dump(self, fh, protocol=protocol)

        # Atomic transaction.
        #import shutil
        #filepath_new = filepath + ".new"
        #filepath_save = filepath + ".save"
        #shutil.copyfile(filepath, filepath_save)

        #try:
        #    with open(filepath_new, mode="w" if protocol == 0 else "wb") as fh:
        #        pickle.dump(self, fh, protocol=protocol)

        #    os.rename(filepath_new, filepath)
        #except:
        #    os.rename(filepath_save, filepath)
        #finally:
        #    try
        #        os.remove(filepath_save)
        #    except:
        #        pass
        #    try
        #        os.remove(filepath_new)
        #    except:
        #        pass

    @staticmethod
    def pickle_load(filepath):
        with open(filepath, "rb") as fh:
            flow = pickle.load(fh)

        flow.connect_signals()
        return flow

    def register_work(self, work, deps=None, manager=None):
        """
        Register a new `Workflow` and add it to the internal list, 
        taking into account possible dependencies.

        Args:
            work:
                `Workflow` object.
            deps:
                List of `Dependency` objects specifying the dependency of this node.
                An empy list of deps implies that this node has no dependencies.
            manager:
                The `TaskManager` responsible for the submission of the task. 
                If manager is None, we use the `TaskManager` specified during the creation of the workflow.

        Returns:   
            The workflow.
        """
        # Directory of the workflow.
        work_workdir = os.path.join(self.workdir, "work_" + str(len(self)))

        # Make a deepcopy since manager is mutable and we might change it at run-time.
        manager = self.manager.deepcopy() if manager is None else manager.deepcopy()
        print("flow manager", manager)

        work.set_workdir(work_workdir)
        work.set_manager(manager)

        self.works.append(work)

        if deps:
            deps = [Dependency(node, exts) for node, exts in deps.items()]
            work.add_deps(deps)

        return work

    def register_cbk(self, cbk, cbk_data, deps, work_class, manager=None):
        """
        Registers a callback function that will generate the Task of the workflow.

        Args:
            cbk:
                Callback function.
            cbk_data
                Callback function.
            deps:
                List of `Dependency` objects specifying the dependency of the workflow.
            work_class:
                `Workflow` class to instantiate.
            manager:
                The `TaskManager` responsible for the submission of the task. 
                If manager is None, we use the `TaskManager` specified during the creation of the `Flow`.
                                                                                                            
        Returns:   
            The `Workflow` that will be finalized by the callback.
        """
        # Directory of the workflow.
        work_workdir = os.path.join(self.workdir, "work_" + str(len(self)))
                                                                                                            
        # Make a deepcopy since manager is mutable and we might change it at run-time.
        manager = self.manager.deepcopy() if manager is None else manager.deepcopy()
                                                                                                            
        # Create an empty workflow and register the callback
        work = work_class(workdir=work_workdir, manager=manager)
        
        self._works.append(work)
                                                                                                            
        deps = [Dependency(node, exts) for node, exts in deps.items()]
        if not deps:
            raise ValueError("A callback must have deps!")

        work.add_deps(deps)

        # Wrap the callable in a Callback object and save 
        # useful info such as the index of the workflow and the callback data.
        cbk = Callback(cbk, work, deps=deps, cbk_data=cbk_data)
                                                                                                            
        self._callbacks.append(cbk)
                                                                                                            
        return work

    def allocate(self):
        for work in self:
            work.allocate()

        return self

    def show_dependencies(self):
        for work in self:
            work.show_intrawork_deps()

    def on_dep_ok(self, signal, sender):
        print("on_dep_ok with sender %s, signal %s" % (str(sender), signal))

        for i, cbk in enumerate(self._callbacks):

            if not cbk.handle_sender(sender):
                print("Do not handle")
                continue

            if not cbk.can_execute():
                print("cannot execute")
                continue 

            # Execute the callback to generate the workflow.
            print("about to build new workflow")
            #empty_work = self._works[cbk.w_idx]

            # TODO better treatment of ids
            # Make sure the new workflow has the same id as the previous one.
            #new_work_idx = cbk.w_idx
            work = cbk(flow=self)
            work.add_deps(cbk.deps)

            # Disable the callback.
            cbk.disable()

            # Update the database.
            self.pickle_dump()

    def connect_signals(self):
        """
        Connect the signals within the workflow.
        self is responsible for catching the important signals raised from 
        its task and raise new signals when some particular condition occurs.
        """
        # Connect the signals inside each Workflow.
        for work in self:
            work.connect_signals()

        # Observe the nodes that must reach S_OK in order to call the callbacks.
        for cbk in self._callbacks:
            for dep in cbk.deps:
                print("connecting %s \nwith sender %s, signal %s" % (str(cbk), dep.node, dep.node.S_OK))
                dispatcher.connect(self.on_dep_ok, signal=dep.node.S_OK, sender=dep.node, weak=False)

        self.show_receivers()

    def show_receivers(self, sender=dispatcher.Any, signal=dispatcher.Any):
        print("*** live receivers ***")
        for rec in dispatcher.liveReceivers(dispatcher.getReceivers(sender, signal)):
            print("receiver -->", rec)
        print("*** end live receivers ***")


class Callback(object):

    def __init__(self, func, work, deps, cbk_data):
        """
        Initialize the callback.

        Args:
            func:
            work:
            deps:
            cbk_data:
        """
        self.func  = func
        self.work = work
        self.deps = deps
        self.cbk_data = cbk_data or {}
        self._disabled = False

    def __call__(self, flow):
        """Execute the callback."""
        if self.can_execute():
            print("in callback")
            #print("in callback with sender %s, signal %s" % (sender, signal))
            cbk_data = self.cbk_data.copy()
            #cbk_data["_w_idx"] = self.w_idx
            return self.func(flow=flow, work=self.work, cbk_data=cbk_data)
        else:
            raise Exception("Cannot execute")

    def can_execute(self):
        """True if we can execut the callback."""
        return not self._disabled and [dep.status == Task.S_OK  for dep in self.deps]

    def disable(self):
        """
        True if the callback has been disabled.
        This usually happens when the callback has been executed.
        """
        self._disabled = True

    def handle_sender(self, sender):
        """
        True if the callback is associated to the sender
        i.e. if the node who sent the signal appears in the 
        dependecies of the callback.
        """
        return sender in [d.node for d in self.deps]