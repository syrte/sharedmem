"""
    Easier parallel programming on shared memory computers.

    The source code is at http://github.com/rainwoodman/sharedmem .

    .. contents:: Topics
        :local:

    Programming Model
    -----------------
    :py:class:`MapReduce` provides the equivalent to multiprocessing.Pool, with the following
    differences:

    - MapReduce does not require the work function to be picklable.
    - MapReduce adds a reduction step that is guaranteed to run on the master process's
      scope.
    - MapReduce allows the use of critical sections and ordered execution in the work
      function.

    Modifications to shared Memory arrays, allocated via 

    - :py:meth:`sharedmem.empty`,
    - :py:meth:`sharedmem.empty_like`,
    - :py:meth:`sharedmem.copy`,

    are visible by all processes, including the master process.

    Usage
    -----
    The package can be installed via :code:`easy_install sharedmem`.
    Alternatively, the file :code:`sharedmem.py` can be directly embedded into 
    other projects.

    The only external dependency is numpy, since this was designed to
    work with large shared memory chunks through numpy.ndarray.

    Environment variable OMP_NUM_THREADS is used to determine the
    default number of slaves.

    Notes
    -----
    This module depends on the `fork` system call, thus is available
    only on posix systems (not Windows).

    Examples
    --------

    Sum up a large array

    >>> input = numpy.arange(1024 * 1024 * 128, dtype='f8')
    >>> output = sharedmem.empty(1024 * 1024 * 128, dtype='f8')
    >>> with MapReduce() as pool:
    >>>    chunksize = 1024 * 1024
    >>>    def work(i):
    >>>        s = slice (i, i + chunksize)
    >>>        output[s] = input[s]
    >>>        return i, sum(input[s])
    >>>    def reduce(i, r):
    >>>        print('chunk', i, 'done')
    >>>        return r
    >>>    r = pool.map(work, range(0, len(input), chunksize), reduce=reduce)
    >>> print numpy.sum(r)
    >>>

    Textual analysis

    >>> input = file('mytextfile.txt').readlines()
    >>> word_count = {'bacon': 0, 'eggs': 0 }
    >>> with MapReduce() as pool:
    >>>    def work(line):
    >>>        words = line.split()
    >>>        for word in words:
    >>>            word_count[word] += 1
    >>>        return word_count
    >>>    def reduce(wc):
    >>>        for key in word_count:
    >>>            word_count[key] += wc[key]
    >>> print word_count
    >>>

    pool.ordered can be used to require a block of code to be executed in order
    
    >>> with MapReduce() as pool:
    >>>    def work(i):
    >>>         with pool.ordered:
    >>>            print(i)
    >>>    pool.map(work, range(10))

    pool.critical can be used to require a block of code to be executed in a critical
    section.

    >>> counter = sharedmem.empty(1)
    >>> counter[:] = 0
    >>> with MapReduce() as pool:
    >>>    def work(i):
    >>>         with pool.critical:
    >>>             counter[:] += i
    >>>    pool.map(work, range(10))
    >>> print(counter)

    API References
    --------------
    
"""
__author__ = "Yu Feng"
__email__ = "rainwoodman@gmail.com"

__all__ = ['set_debug', 'get_debug', 
        'total_memory', 'cpu_count', 
        'SlaveException', 'StopProcessGroup',
        'background',
        'MapReduce', 'MapReduceByThread',
        'empty', 'empty_like', 'copy',
        ]

import os
import multiprocessing
import threading
try:
    import Queue as queue
except ImportError:
    import queue

from collections import deque
import traceback
import warnings
import gc
import threading
import heapq
import os

try:
    import cPickle as pickle
except ImportError:
    import pickle

import numpy
from multiprocessing import RawArray
import ctypes
import mmap
#logger = multiprocessing.log_to_stderr()
#logger.setLevel(multiprocessing.SUBDEBUG)

__shmdebug__ = False

def set_debug(flag):
    """ Set the debug mode.

        In debug mode (flag==True), no slaves are spawn.
        All work are done in serial on the master thread/process.
        This eases debuggin when the worker throws out an exception. 
    """
    global __shmdebug__
    __shmdebug__ = flag

def get_debug():
    """ Get the debug mode """    
    global __shmdebug__
    return __shmdebug__

def total_memory():
    """ Returns the the amount of memory available for use.

        This function is not very useful.
        The memory is obtained from MemTotal entry in /proc/meminfo.

    """
    with file('/proc/meminfo', 'r') as f:
        for line in f:
            words = line.split()
        if words[0].upper() == 'MEMTOTAL:':
            return int(words[1]) * 1024
    raise IOError('MemTotal unknown')

def cpu_count():
    """ Returns the number of slave processes to be spawned.

        The default value is the number of physical cpu cores seen by python.
        :code:`OMP_NUM_THREADS` environment variable overrides it.

        On PBS/torque systems if OMP_NUM_THREADS is empty, we try to
        use the value of :code:`PBS_NUM_PPN` variable.

        Notes
        -----
        On some machines the physical number of cores does not equal
        the number of cpus shall be used. PSC Blacklight for example.

    """
    num = os.getenv("OMP_NUM_THREADS")
    if num is None:
        num = os.getenv("PBS_NUM_PPN")
    try:
        return int(num)
    except:
        return multiprocessing.cpu_count()
class LostExceptionType(Warning):
    pass

class SlaveException(Exception):
    """ Represents an exception that has occured during a slave process 

        Attributes
        ----------
        reason : Exception, or subclass of Exception.
            The underlining reason of the exception.
            If the original exception can be pickled, the type of the exception
            is preserved. Otherwise, a LostExceptionType warning is issued, and
            reason is of type Exception.

        traceback : str
            The string version of the traceback that can be used to inspect the 
            error.

    """
    def __init__(self, reason, traceback):
        if not isinstance(reason, Exception):
            warnings.warn("Type information of Unpicklable exception %s is lost" % reason, LostExceptionType)
            reason = Exception(reason)
        self.reason = reason
        self.traceback = traceback
        Exception.__init__(self, "%s\n%s" % (str(reason), str(traceback)))

class StopProcessGroup(Exception):
    """ StopProcessGroup will terminate the slave process/thread """
    def __init__(self):
        Exception.__init__(self, "StopProcessGroup")

class ProcessGroup(object):
    """ Monitoring a group of worker processes """
    def __init__(self, backend, main, np, args=()):
        self.Errors = backend.QueueFactory(1)
        self._tls = backend.StorageFactory()
        self.main = main
        self.args = args
        self.guard = threading.Thread(target=self._guardMain)
        self.errorguard = threading.Thread(target=self._errorGuard)
        # this has to be from backend because the slaves will check
        # this variable.

        self.guardDead = backend.EventFactory()
        # each dead child releases one sempahore
        # when all dead guard will proceed to set guarddead
        self.semaphore = threading.Semaphore(0)
        self.JoinedProcesses = multiprocessing.RawValue('l')
        self.P = [
            backend.SlaveFactory(target=self._slaveMain,
                args=(rank,)) \
                for rank in range(np)
            ]
        self.G = [
            threading.Thread(target=self._slaveGuard,
                args=(rank, self.P[rank])) \
                for rank in range(np)
            ]
        return

    def _slaveMain(self, rank):
        self._tls.rank = rank
        try:
            self.main(self, *self.args)
        except SlaveException as e:
            raise RuntimError("slave exception shall never be caught by a slave")
        except StopProcessGroup as e:
            pass
        except BaseException as e:
            try:
                # Put in the string version of the exception,
                # Some of the Exception types in extension types are probably
                # not picklable (thus can't be sent via a queue), 
                # However, we don't use the extra information in customized
                # Exception types anyways.
                try:
                    pickle.dumps(e)
                except Exception as ee:
                    e = str(e)
                 
                tb = traceback.format_exc()
                self.Errors.put((e, tb), timeout=0)
            except queue.Full:
                pass
        finally:
#            self.Errors.close()
#            self.Errors.join_thread()
            # making all slaves exit one after another
            # on some Linuxes if many slaves (56+) access
            # mmap randomly the termination of the slaves
            # run into a deadlock.
            while self.JoinedProcesses.value < rank:
                continue
            pass

    def killall(self):
        for p in self.P:
            if not p.is_alive(): continue
            try:
                if isinstance(p, threading.Thread): p.join()
                else: os.kill(p._popen.pid, 5)
            except Exception as e:
                print(e)
                continue

    def _errorGuard(self):
        # this guard will kill every child if
        # an error is observed. We watch for this every 0.5 seconds
        # (errors do not happen very often)
        # if guardDead is set or killall is emitted, this will end immediately.
        while not self.guardDead.is_set():
            if not self.Errors.empty():
                self.killall()
                break
            # for python 2.6.x wait returns None XXX
            self.guardDead.wait(timeout=0.5)

    def _slaveGuard(self, rank, process):
        process.join()
        if isinstance(process, threading.Thread):
            pass
        else:
            if process.exitcode < 0 and process.exitcode != -5:
                e = Exception("slave process %d killed by signal %d" % (rank, -
                    process.exitcode))
                try:
                    self.Errors.put((e, ""), timeout=0)
                except queue.Full:
                    pass
        self.semaphore.release() 

    def _guardMain(self):
        # this guard will wait till all children are dead.
        # we then set the guardDead event
        for x in self.G:
            self.semaphore.acquire()
            self.JoinedProcesses.value = self.JoinedProcesses.value + 1

        self.guardDead.set()

    def start(self):
        self.JoinedProcesses.value = 0
        self.guardDead.clear()

        # collect the garbages before forking so that the left-over
        # junk won't throw out assertion errors due to
        # wrong pid in multiprocess.heap
        gc.collect()

        for x in self.P:
            x.start()

        # p is alive from the moment start returns.
        # thus we can join them immediately after start returns.
        # guardMain will check if the slave has been
        # killed by the os, and simulate an error if so.
        for x in self.G:
            x.start()
        self.errorguard.start()
        self.guard.start()

    def get_exception(self):
        exp = self.Errors.get(timeout=0)
        return SlaveException(*exp)

    def get(self, Q):
        """ Protected get. Get an item from Q.
            Will block. but if the process group has errors,
            raise an StopProcessGroup exception.

            A slave process will terminate upon StopProcessGroup.
            The master process shall read the error
        """
        while self.Errors.empty():
            try:
                return Q.get(timeout=1)
            except queue.Empty:
                if not self.is_alive():
                    raise StopProcessGroup
                else:
                    continue
        else:
            raise StopProcessGroup

    def put(self, Q, item):
        while self.Errors.empty():
            try:
                Q.put(item, timeout=1)
                return
            except queue.Full:
                if not self.is_alive():
                    raise StopProcessGroup
                else:
                    continue
        else:
            raise StopProcessGroup

    def is_alive(self):
        return not self.guardDead.is_set()

    def join(self):
        self.guardDead.wait()
        for x in self.G:
            x.join()

        self.errorguard.join()
        self.guard.join()
        if not self.Errors.empty():
            raise SlaveException(*self.Errors.get())

class Ordered(object):
    def __init__(self, backend):
      #  self.counter = lambda : None
        #multiprocessing.RawValue('l')
        self.event = backend.EventFactory()
        self.counter = multiprocessing.RawValue('l')
        self.tls = backend.StorageFactory()

    def reset(self):
        self.counter.value = 0
        self.event.set()

    def move(self, iter):
        self.tls.iter = iter

    def __enter__(self):
        while self.counter.value != self.tls.iter:
            self.event.wait() 
        self.event.clear()
        return self

    def __exit__(self, *args):
        # increase counter before releasing the value
        # so that the others waiting will see the new counter
        self.counter.value = self.counter.value + 1
        self.event.set()


class ThreadBackend:
      QueueFactory = staticmethod(queue.Queue)
      EventFactory = staticmethod(threading.Event)
      LockFactory = staticmethod(threading.Lock)
      StorageFactory = staticmethod(threading.local)
      @staticmethod
      def SlaveFactory(*args, **kwargs):
        slave = threading.Thread(*args, **kwargs)
        slave.daemon = True
        return slave

class ProcessBackend:
      QueueFactory = staticmethod(multiprocessing.Queue)
      EventFactory = staticmethod(multiprocessing.Event)
      LockFactory = staticmethod(multiprocessing.Lock)

      @staticmethod
      def SlaveFactory(*args, **kwargs):
        slave = multiprocessing.Process(*args, **kwargs)
        slave.daemon = True
        return slave
      @staticmethod
      def StorageFactory():
          return lambda:None

class background(object):
    """ Asyncrhonized function call via a background process.

        Parameters
        ----------
        function : callable
            the function to call
        *args   : positional arguments
        **kwargs : keyward arguments

        Examples
        --------

        >>> def function(*args, **kwargs):
        >>>    pass
        >>> bg = background(function, *args, **kwargs)
        >>> rt = bg.wait()

    """
    def __init__(self, function, *args, **kwargs):
            
        backend = kwargs.pop('backend', ProcessBackend)

        self.result = backend.QueueFactory(1)
        self.slave = backend.SlaveFactory(target=self._closure, 
                args=(function, args, kwargs, self.result))
        self.slave.start()

    def _closure(self, function, args, kwargs, result):
        try:
            rt = function(*args, **kwargs)
        except Exception as e:
            result.put((e, traceback.format_exc()))
        else:
            result.put((None, rt))

    def wait(self):
        """ Wait and join the child process. 
            The return value of the function call is returned.
            If any exception occurred it is wrapped and raised.
        """
        e, r = self.result.get()
        self.slave.join()
        self.slave = None
        self.result = None
        if isinstance(e, Exception):
            raise SlaveException(e, r)
        return r

def MapReduceByThread(np=None):
    """ Creates a MapReduce object but with the Thread backend.

        The process backend is usually preferred.
    """
    return MapReduce(backend=ThreadBackend, np=np)

class MapReduce(object):
    """
        A pool of slave processes for a Map-Reduce operation

        Parameters
        ----------
        backend : ProcessBackend or ThreadBackend
            ProcessBackend is preferred. ThreadBackend can be used in cases where
            processes creation is not allowed.

        np   : int or None
            Number of processes to use. Default (None) is from OMP_NUM_THREADS or
            the number of available cores on the computer. If np is 0, all operations
            are performed on the master process -- no child processes are created.

        Notes
        -----
        Always wrap the call to :py:meth:`map` in a context manager ('with') block.

    """
    def __init__(self, backend=ProcessBackend, np=None):
        self.backend = backend
        if np is None:
            self.np = cpu_count()
        else:
            self.np = np

    def _main(self, pg, Q, R, sequence, realfunc):
        # get and put will raise SlaveException
        # and terminate the process.
        # the exception is muted in ProcessGroup,
        # as it will only be dispatched from master.
        while True:
            capsule = pg.get(Q)
            if capsule is None:
                return
            if len(capsule) == 1:
                i, = capsule
                work = sequence[i]
            else:
                i, work = capsule
            self.ordered.move(i)
            r = realfunc(work)
            pg.put(R, (i, r))

    def __enter__(self):
        self.critical = self.backend.LockFactory()
        self.ordered = Ordered(self.backend)
        return self

    def __exit__(self, *args):
        self.ordered = None
        pass

    def map(self, func, sequence, reduce=None, star=False):
        """ Map-reduce with multile processes.

            Apply func to each item on the sequence, in parallel. 
            As the results are collected, reduce is called on the result.
            The reduced result is returned as a list.
            
            Parameters
            ----------
            func : callable
                The function to call. It must accept the same number of
                arguments as the length of an item in the sequence.

                .. warning::

                    func is not supposed to use exceptions for flow control.
                    In non-debug mode all exceptions will be wrapped into
                    a :py:class:`SlaveException`.

            sequence : list or array_like
                The sequence of arguments to be applied to func.

            reduce : callable, optional
                Apply an reduction operation on the 
                return values of func. If func returns a tuple, they
                are treated as positional arguments of reduce.

            star : boolean
                if True, the items in sequence are treated as positional
                arguments of reduce.

            Returns
            -------
            results : list
                The list of reduced results from the map operation, in
                the order of the arguments of sequence.
                
            Raises
            ------
            SlaveException
                If any of the slave process encounters
                an exception. Inspect :py:attr:`SlaveException.reason` for the underlying exception.
        
        """ 
        def realreduce(r):
            if reduce:
                if isinstance(r, tuple):
                    return reduce(*r)
                else:
                    return reduce(r)
            return r

        def realfunc(i):
            if star: return func(*i)
            else: return func(i)

        if self.np == 0 or get_debug():
            #Do this in serial
            return [realreduce(realfunc(i)) for i in sequence]

        Q = self.backend.QueueFactory(64)
        R = self.backend.QueueFactory(64)
        self.ordered.reset()

        pg = ProcessGroup(main=self._main, np=self.np,
                backend=self.backend,
                args=(Q, R, sequence, realfunc))

        pg.start()

        L = []
        N = []
        def feeder(pg, Q, N):
            #   will fail silently if any error occurs.
            j = 0
            try:
                for i, work in enumerate(sequence):
                    if not hasattr(sequence, '__getitem__'):
                        pg.put(Q, (i, work))
                    else:
                        pg.put(Q, (i, ))
                    j = j + 1
                N.append(j)

                for i in range(self.np):
                    pg.put(Q, None)
            except StopProcessGroup:
                return
            finally:
                pass
        feeder = threading.Thread(None, feeder, args=(pg, Q, N))
        feeder.start() 

        # we run fetcher on main thread to catch exceptions
        # raised by reduce 
        count = 0
        try:
            while True:
                try:
                    capsule = pg.get(R)
                except queue.Empty:
                    continue
                except StopProcessGroup:
                    raise pg.get_exception()
                capsule = capsule[0], realreduce(capsule[1])
                heapq.heappush(L, capsule)
                count = count + 1
                if len(N) > 0 and count == N[0]: 
                    # if finished feeding see if all
                    # results have been obtained
                    break
            rt = []
#            R.close()
#            R.join_thread()
            while len(L) > 0:
                rt.append(heapq.heappop(L)[1])
            pg.join()
            feeder.join()
            assert N[0] == len(rt)
            return rt
        except BaseException as e:
            pg.killall()
            pg.join()
            feeder.join()
            raise 


def empty_like(array, dtype=None):
    """ Create a shared memory array from the shape of array.
    """
    if dtype is None: dtype = array.dtype
    return anonymousmemmap(numpy.broadcast(array, array).shape, dtype)

def empty(shape, dtype='f8'):
    """ Create an empty shared memory array.
    """
    return anonymousmemmap(shape, dtype)

def copy(a):
    """ Copy an array to the shared memory. 

        Notes
        -----
        copy is not always necessary because the private memory is always copy-on-write.

        Use :code:`a = copy(a)` to immediately dereference the old 'a' on private memory
    """
    shared = anonymousmemmap(a.shape, dtype=a.dtype)
    shared[:] = a[:]
    return shared

def fromiter(iter, dtype, count=None):
    return copy(numpy.fromiter(iter, dtype, count))

def __unpickle__(ai, dtype):
    dtype = numpy.dtype(dtype)
    tp = numpy.ctypeslib._typecodes['|u1']

    # if there are strides, use strides, otherwise the stride is the itemsize of dtype
    if ai['strides']:
        tp *= ai['strides'][-1]
    else:
        tp *= dtype.itemsize

    for i in numpy.asarray(ai['shape'])[::-1]:
        tp *= i

    # grab a flat char array at the sharemem address, with length at least contain ai required
    ra = tp.from_address(ai['data'][0])
    buffer = numpy.ctypeslib.as_array(ra).ravel()
    # view it as what it should look like
    shm = numpy.ndarray(buffer=buffer, dtype=dtype, 
            strides=ai['strides'], shape=ai['shape']).view(type=anonymousmemmap)
    return shm

class anonymousmemmap(numpy.memmap):
    """ Arrays allocated on shared memory. 

        The array is stored in an anonymous memory map that is shared between child-processes.

    """
    def __new__(subtype, shape, dtype=numpy.uint8, order='C'):

        descr = numpy.dtype(dtype)
        _dbytes = descr.itemsize

        shape = numpy.atleast_1d(shape)
        size = 1
        for k in shape:
            size *= k

        bytes = int(size*_dbytes)

        if bytes > 0:
            mm = mmap.mmap(-1, bytes)
        else:
            mm = numpy.empty(0, dtype=descr)
        self = numpy.ndarray.__new__(subtype, shape, dtype=descr, buffer=mm, order=order)
        self._mmap = mm
        return self
        
    def __array_wrap__(self, outarr, context=None):
    # after ufunc this won't be on shm!
        return numpy.ndarray.__array_wrap__(self.view(numpy.ndarray), outarr, context)

    def __reduce__(self):
        return __unpickle__, (self.__array_interface__, self.dtype)


