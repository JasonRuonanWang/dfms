#
#    ICRAR - International Centre for Radio Astronomy Research
#    (c) UWA - The University of Western Australia, 2014
#    Copyright by UWA (in the framework of the ICRAR)
#    All rights reserved
#
#    This library is free software; you can redistribute it and/or
#    modify it under the terms of the GNU Lesser General Public
#    License as published by the Free Software Foundation; either
#    version 2.1 of the License, or (at your option) any later version.
#
#    This library is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
#    Lesser General Public License for more details.
#
#    You should have received a copy of the GNU Lesser General Public
#    License along with this library; if not, write to the Free Software
#    Foundation, Inc., 59 Temple Place, Suite 330, Boston,
#    MA 02111-1307  USA
#
# Who                   When          What
# ------------------------------------------------
# chen.wu@icrar.org   15/Feb/2015     Created
#
"""
Module containing the core DROP classes.
"""

from abc import ABCMeta, abstractmethod
from cStringIO import StringIO
import collections
import heapq
import logging
import os
import random
import sys
import threading
import time

from ddap_protocol import DROPStates
from dfms.ddap_protocol import ExecutionMode, ChecksumTypes, AppDROPStates,\
    DROPLinkType
from dfms.events.event_broadcaster import LocalEventBroadcaster
from dfms.io import OpenMode, FileIO, MemoryIO, NgasIO, ErrorIO, NullIO


try:
    from crc32c import crc32  # @UnusedImport
    _checksumType = ChecksumTypes.CRC_32C
except:
    from binascii import crc32  # @Reimport
    _checksumType = ChecksumTypes.CRC_32


logger = logging.getLogger(__name__)

#===============================================================================
# DROP classes follow
#===============================================================================


class AbstractDROP(object):
    """
    Base class for all DROP implementations.

    A DROP is a representation of a piece of data. DROPs are created,
    written once, potentially read many times, and they finally potentially
    expire and get deleted. Subclasses implement different storage mechanisms
    to hold the data represented by the DROP.

    If the data represented by this DROP is written *through* this object
    (i.e., calling the ``write`` method), this DROP will keep track of the
    data's size and checksum. If the data is written externally, the size and
    checksum can be fed into this object for future reference.

    DROPs can have consumers attached to them. 'Normal' consumers will
    wait until the DROP they 'consume' (their 'input') moves to the
    COMPLETED state and then will consume it, most typically by opening it
    and reading its contents, but any other operation could also be performed.
    How the consumption is triggered depends on the producer's `executionMode`
    flag, which dictates whether it should trigger the consumption itself or
    if it should be manually triggered by an external entity. On the other hand,
    streaming consumers receive the data that is written into its input
    *as it gets written*. This mechanism is driven always by the DROP that
    acts as a streaming input. Apart from receiving the data as it gets
    written into the DROP, streaming consumers are also notified when the
    DROPs moves to the COMPLETED state, at which point no more data should
    be expected to arrive at the consumer side.
    """

    # This ensures that:
    #  - This class cannot be instantiated
    #  - Subclasses implement methods decorated with @abstractmethod
    __metaclass__ = ABCMeta

    def __init__(self, oid, uid, **kwargs):
        """
        Creates a DROP. The only mandatory argument are the Object ID
        (`oid`) and the Unique ID (`uid`) of the new object (see the `self.oid`
        and `self.uid` methods for more information). Any extra arguments must
        be keyed, and will be processed either by this method, or by the
        `initialize` method.

        This method should not be overwritten by subclasses. For any specific
        initialization logic, the `initialize` method should be overwritten
        instead. This allows us to move to the INITIALIZED state only after any
        specific initialization has occurred in the subclasses.
        """

        super(AbstractDROP, self).__init__()

        # Copy it since we're going to modify it
        kwargs = dict(kwargs)

        # So far only these three are mandatory
        self._oid = str(oid)
        self._uid = str(uid)

        self._bcaster = LocalEventBroadcaster()

        # 1-to-N relationship: one DROP may have many consumers and producers.
        # The potential consumers and producers are always AppDROPs instances
        self._consumers = []
        self._producers = []

        # Set holding the UID of the producers that have finished their
        # execution. Once all producers have finished, this DROP moves
        # itself to the COMPLETED state
        self._finishedProducers = set()
        self._finishedProducersLock = threading.Lock()

        # Streaming consumers are objects that consume the data written in
        # this DROP *as it gets written*, and therefore don't have to
        # wait until this DROP has moved to COMPLETED.
        # An object cannot be a streaming consumers and a 'normal' consumer
        # at the same time, although this rule is imposed simply to enforce
        # efficiency (why would a consumer want to consume the data twice?) and
        # not because it's technically impossible.
        self._streamingConsumers = []

        self._refCount = 0
        self._refLock  = threading.Lock()
        self._location = None
        self._parent   = None
        self._status   = None
        self._statusLock = threading.RLock()
        self._phase    = None

        # Calculating the checksum and maintaining the data size internally
        # implies that the data represented by this DROP is written
        # *through* this DROP. This might not always be the case though,
        # since data could be written externally and the DROP simply be
        # moved to COMPLETED at the end of the process. In this case we return a
        # None checksum and size (when requested), signaling that we don't have
        # this information.
        # Note also that the setters of these two properties also allow to set
        # a value on them, but only if they are None
        self._checksum     = None
        self._checksumType = None
        self._size         = None

        # The DataIO instance we use in our write method. It's initialized to
        # None because it's lazily initialized in the write method, since data
        # might be written externally and not through this DROP
        self._wio = None

        # DataIO objects used for reading.
        # Instead of passing file objects or more complex data types in our
        # open/read/close calls we use integers, mainly because Pyro doesn't
        # handle file types and other classes (like StringIO) well, but also
        # because it requires less transport.
        # TODO: Make these threadsafe, no lock around them yet
        self._rios = {}

        # Maybe we want to have a different default value for this one?
        self._executionMode = self._getArg(kwargs, 'executionMode', ExecutionMode.DROP)

        # The physical node where this DROP resides.
        # This piece of information is mandatory when submitting the physical
        # graph via the DataIslandManager, but in simpler scenarios such as
        # tests or graph submissions via the DROPManager it might be
        # missing. We thus default to '127.0.0.1'
        self._node = self._getArg(kwargs, 'node', '127.0.0.1')

        # Expected lifespan for this object, used by to expire them
        lifespan = -1
        if kwargs.has_key('lifespan'):
            lifespan = float(kwargs.pop('lifespan'))
        self._expirationDate = -1
        if lifespan != -1:
            self._expirationDate = time.time() + lifespan

        # Expected data size, used to automatically move the DROP to COMPLETED
        # after successive calls to write()
        self._expectedSize = -1
        if kwargs.has_key('expectedSize') and kwargs['expectedSize']:
            self._expectedSize = int(kwargs.pop('expectedSize'))

        # All DROPs are precious unless stated otherwise; used for replication
        self._precious = True
        if kwargs.has_key('precious'):
            self._precious = bool(kwargs.pop('precious'))

        try:
            self.initialize(**kwargs)
            self._status = DROPStates.INITIALIZED # no need to use synchronised self.status here
        except:
            # It doesn't make sense to set an internal status here because
            # the creation of the object is actually raising an exception,
            # and the object doesn't get created and assigned to any variable
            # Still, the FAILED state could be used for other purposes, like
            # failure during writing for example.
            raise

    def _getArg(self, kwargs, key, default):
        val = default
        if kwargs.has_key(key):
            val = kwargs.pop(key)
        elif logger.isEnabledFor(logging.DEBUG):
            logger.debug("Defaulting %s to %s" % (key, str(val)))
        return val

    def __hash__(self):
        return hash(self._uid)

    def __repr__(self):
        re = "%s %s/%s" % (self.__class__.__name__, self.oid, self.uid)
        if self.location:
            re += "@{0}".format(self.location)
        return re

    def initialize(self, **kwargs):
        """
        Performs any specific subclass initialization.

        `kwargs` contains all the keyword arguments given at construction time,
        except those used by the constructor itself. Implementations of this
        method should make sure that arguments in the `kwargs` dictionary are
        removed once they are interpreted so they are not interpreted by
        accident by another method implementations that might reside in the call
        hierarchy (in the case that a subclass implementation calls the parent
        method implementation, which is usually the case).
        """

    def incrRefCount(self):
        """
        Increments the reference count of this DROP by one atomically.
        """
        with self._refLock:
            self._refCount += 1

    def decrRefCount(self):
        """
        Decrements the reference count of this DROP by one atomically.
        """
        with self._refLock:
            self._refCount -= 1

    def open(self, **kwargs):
        """
        Opens the DROP for reading, and returns a "DROP descriptor"
        that must be used when invoking the read() and close() methods.
        DROPs maintain a internal reference count based on the number
        of times they are opened for reading; because of that after a successful
        call to this method the corresponding close() method must eventually be
        invoked. Failing to do so will result in DROPs not expiring and
        getting deleted.
        """
        # TODO: We could also allow opening EXPIRED DROPs, in which case
        # it could trigger its "undeletion", but this would require an automatic
        # recalculation of its new expiration date, which is maybe something we
        # don't have to have
        if self.status != DROPStates.COMPLETED:
            raise Exception("DROP %s/%s is in state %s (!=COMPLETED), cannot be opened for reading" % (self._oid, self._uid, self.status))

        io = self.getIO()
        io.open(OpenMode.OPEN_READ, **kwargs)

        # Save the IO object in the dictionary and return its descriptor instead
        while True:
            descriptor = random.SystemRandom().randint(-sys.maxint - 1, sys.maxint)
            if descriptor not in self._rios:
                break
        self._rios[descriptor] = io

        # This occurs only after a successful opening
        self.incrRefCount()
        self._fire('open')

        return descriptor

    def close(self, descriptor, **kwargs):
        """
        Closes the given DROP descriptor, decreasing the DROP's
        internal reference count and releasing the underlying resources
        associated to the descriptor.
        """
        self._checkStateAndDescriptor(descriptor)

        # Decrement counter and then actually close
        self.decrRefCount()
        io = self._rios.pop(descriptor)
        io.close(**kwargs)

    def read(self, descriptor, count=4096, **kwargs):
        """
        Reads `count` bytes from the given DROP `descriptor`.
        """
        self._checkStateAndDescriptor(descriptor)
        io = self._rios[descriptor]
        return io.read(count, **kwargs)

    def _checkStateAndDescriptor(self, descriptor):
        if self.status != DROPStates.COMPLETED:
            raise Exception("DROP %s/%s is in state %s (!=COMPLETED), cannot be read" % (self._oid, self._uid, self.status))
        if descriptor not in self._rios:
            raise Exception("Illegal descriptor %d given, remember to open() first" % (descriptor))

    def isBeingRead(self):
        """
        Returns `True` if the DROP is currently being read; `False`
        otherwise
        """
        with self._refLock:
            return self._refCount > 0

    def write(self, data, **kwargs):
        '''
        Writes the given `data` into this DROP. This method is only meant
        to be called while the DROP is in INITIALIZED or WRITING state;
        once the DROP is COMPLETE or beyond only reading is allowed.
        The underlying storage mechanism is responsible for implementing the
        final writing logic via the `self.writeMeta()` method.
        '''

        if self.status not in [DROPStates.INITIALIZED, DROPStates.WRITING]:
            raise Exception("No more writing expected")

        # We lazily initialize our writing IO instance because the data of this
        # DROP might not be written through this DROP
        if not self._wio:
            self._wio = self.getIO()
            self._wio.open(OpenMode.OPEN_WRITE)
        nbytes = self._wio.write(data)

        dataLen = len(data)
        if nbytes != dataLen:
            # TODO: Maybe this should be an actual error?
            logger.warn('Not all data was correctly written by %s (%d/%d bytes written)' % (self, nbytes, dataLen))

        # see __init__ for the initialization to None
        if self._size is None:
            self._size = 0
        self._size += nbytes

        # Trigger our streaming consumers
        if self._streamingConsumers:
            writtenData = buffer(data, 0, nbytes)
            for streamingConsumer in self._streamingConsumers:
                streamingConsumer.dataWritten(self.uid, writtenData)

        # Update our internal checksum
        self._updateChecksum(data)

        # If we know how much data we'll receive, keep track of it and
        # automatically switch to COMPLETED
        if self._expectedSize > 0:
            remaining = self._expectedSize - self._size
            if remaining > 0:
                self.status = DROPStates.WRITING
            else:
                if remaining < 0:
                    logger.warning("Received and wrote more bytes than expected: " + str(-remaining))
                logger.debug("Automatically moving DROP %s/%s to COMPLETED, all expected data arrived" % (self.oid, self.uid))
                self.setCompleted()
        else:
            self.status = DROPStates.WRITING

        return nbytes

    @abstractmethod
    def getIO(self):
        """
        Returns an instance of one of the `dfms.io.DataIO` instances that
        handles the data contents of this DROP.
        """

    def delete(self):
        """
        Deletes the data represented by this DROP.
        """
        self.getIO().delete()

    def exists(self):
        """
        Returns `True` if the data represented by this DROP exists indeed
        in the underlying storage mechanism
        """
        return self.getIO().exists()

    @abstractmethod
    def dataURL(self):
        """
        A URL that points to the data referenced by this DROP. Different
        DROP implementations will use different URI schemes.
        """

    def _updateChecksum(self, chunk):
        # see __init__ for the initialization to None
        if self._checksum is None:
            self._checksum = 0
            self._checksumType = _checksumType
        self._checksum = crc32(chunk, self._checksum)

    @property
    def checksum(self):
        """
        The checksum value for the data represented by this DROP. Its
        value is automatically calculated if the data was actually written
        through this DROP (using the `self.write()` method directly or
        indirectly). In the case that the data has been externally written, the
        checksum can be set externally after the DROP has been moved to
        COMPLETED or beyond.

        :see: `self.checksumType`
        """
        return self._checksum

    @checksum.setter
    def checksum(self, value):
        if self._checksum is not None:
            raise Exception("The checksum for DROP %s is already calculated, cannot overwrite with new value" % (self))
        if self.status in [DROPStates.INITIALIZED, DROPStates.WRITING]:
            raise Exception("DROP %s is still not fully written, cannot manually set a checksum yet" % (self))
        self._checksum = value

    @property
    def checksumType(self):
        """
        The algorithm used to compute this DROP's data checksum. Its value
        if automatically set if the data was actually written through this
        DROP (using the `self.write()` method directly or indirectly). In
        the case that the data has been externally written, the checksum type
        can be set externally after the DROP has been moved to COMPLETED
        or beyond.

        :see: `self.checksum`
        """
        return self._checksumType

    @checksumType.setter
    def checksumType(self, value):
        if self._checksumType is not None:
            raise Exception("The checksum type for DROP %s is already set, cannot overwrite with new value" % (self))
        if self.status in [DROPStates.INITIALIZED, DROPStates.WRITING]:
            raise Exception("DROP %s is still not fully written, cannot manually set a checksum type yet" % (self))
        self._checksumType = value

    @property
    def oid(self):
        """
        The DROP's Object ID (OID). OIDs are unique identifiers given to
        semantically different DROPs (and by consequence the data they
        represent). This means that different DROPs that point to the same
        data semantically speaking, either in the same or in a different
        storage, will share the same OID.
        """
        return self._oid

    @property
    def uid(self):
        """
        The DROP's Unique ID (UID). Unlike the OID, the UID is globally
        different for all DROP instances, regardless of the data they
        point to.
        """
        return self._uid

    @property
    def executionMode(self):
        """
        The execution mode of this DROP. If `ExecutionMode.DROP` it means
        that this DROP will automatically trigger the execution of all its
        consumers. If `ExecutionMode.EXTERNAL` it means that this DROP
        will *not* trigger its consumers, and therefore an external entity will
        have to do it.
        """
        return self._executionMode

    def subscribe(self, callback, eventType=None):
        """
        Adds a new subscription to events fired from this DROP.
        """
        self._bcaster.subscribe(callback, eventType=eventType)

    def unsubscribe(self, callback, eventType=None):
        """
        Removes a subscription from events fired from this DROP.
        """
        self._bcaster.unsubscribe(callback, eventType=eventType)

    def handleInterest(self, drop):
        """
        Main mechanism through which a DROP handles its interest in a
        second DROP it isn't directly related to.

        A call to this method should be expected for each DROP this
        DROP is interested in. The default implementation does nothing,
        but implementations are free to perform any action, such as subscribing
        to events or storing information.

        At this layer only the handling of such an interest exists. The
        expression of such interest, and the invocation of this method wherever
        necessary, is currently left as a responsibility of the entity creating
        the DROPs. In the case of a Session in a DROPManager for
        example this step would be performed using deployment-time information
        contained in the dropspec dictionaries held in the session.
        """

    def _fire(self, eventType, **kwargs):
        kwargs['oid'] = self.oid
        kwargs['uid'] = self.uid
        self._bcaster.fire(eventType, **kwargs)

    @property
    def phase(self):
        """
        This DROP's phase. The phase indicates the availability of a
        DROP.
        """
        return self._phase

    @phase.setter
    def phase(self, phase):
        self._phase = phase

    @property
    def expirationDate(self):
        return self._expirationDate

    @property
    def size(self):
        """
        The size of the data pointed by this DROP. Its value is
        automatically calculated if the data was actually written through this
        DROP (using the `self.write()` method directly or indirectly). In
        the case that the data has been externally written, the size can be set
        externally after the DROP has been moved to COMPLETED or beyond.
        """
        return self._size

    @size.setter
    def size(self, size):
        if self._size is not None:
            raise Exception("The size of DROP %s is already calculated, cannot overwrite with new value" % (self))
        if self.status in [DROPStates.INITIALIZED, DROPStates.WRITING]:
            raise Exception("DROP %s is still not fully written, cannot manually set a size yet" % (self))
        self._size = size

    @property
    def precious(self):
        """
        Whether this DROP should be considered as 'precious' or not
        """
        return self._precious

    @property
    def status(self):
        """
        The current status of this DROP.
        """
        with self._statusLock:
            return self._status

    @status.setter
    def status(self, value):
        with self._statusLock:
            # if we are already in the state that is requested then do nothing
            if value == self._status:
                return
            self._status = value

        self._fire('status', status = value)

    @property
    def parent(self):
        """
        The DROP that acts as the parent of the current one. This
        parent/child relationship is created by ContainerDROPs, which are
        a specific kind of DROP.
        """
        return self._parent

    @parent.setter
    def parent(self, parent):
        if self._parent and parent:
            logger.warn("A parent is already set in DROP %s/%s, overwriting with new value" % (self._oid, self._uid))
        if parent:
            prevParent = self._parent
            self._parent = parent # a parent is a container
            if hasattr(parent, 'addChild'):
                try:
                    parent.addChild(self)
                except:
                    self._parent = prevParent

    @property
    def consumers(self):
        """
        The list of 'normal' consumers held by this DROP.

        :see: `self.addConsumer()`
        """
        return self._consumers[:]

    def addConsumer(self, consumer):
        """
        Adds a consumer to this DROP.

        Consumers are normally (but not necessarily) AppDROPs that get
        notified when this DROP moves into the COMPLETED state. This is
        notified by calling the consumer's `dropCompleted` method with the
        UID of this DROP.

        This is one of the key mechanisms by which the DROP graph is
        executed automatically. If AppDROP B consumes DROP A, then
        as soon as A transitions to COMPLETED B will be notified and will
        probably start its execution.
        """

        # Consumers have a "consume" method that gets invoked when
        # this DROP moves to COMPLETED
        if not hasattr(consumer, 'dropCompleted'):
            raise Exception("The consumer %s doesn't have a 'dropCompleted' method, cannot add to %s" % (consumer, self))

        # An object cannot be a normal and streaming consumer at the same time,
        # see the comment in the __init__ method
        if consumer in self._streamingConsumers:
            raise Exception("Consumer %s is already registered as a streaming consumer" % (consumer))

        # Add if not already present
        # Add the reverse reference too automatically
        if consumer in self._consumers:
            return
        logger.debug('Adding new consumer for DROP %s/%s: %s' %(self.oid, self.uid, consumer))
        self._consumers.append(consumer)

        # Automatic back-reference
        if hasattr(consumer, 'addInput'):
            consumer.addInput(self)

        # Only trigger consumers automatically if the DROP graph's
        # execution is driven by the DROPs themselves
        if self._executionMode == ExecutionMode.EXTERNAL:
            return

        def dropCompleted(e):
            if e.status != DROPStates.COMPLETED:
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug('Skipping event for consumer %s: %s' %(consumer, str(e.__dict__)) )
                return
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug('Triggering consumer %s: %s' %(consumer, str(e.__dict__)))

            consumer.dropCompleted(self.uid)
        self.subscribe(dropCompleted, eventType='status')

    @property
    def producers(self):
        """
        The list of producers that write to this DROP

        :see: `self.addProducer()`
        """
        return self._producers[:]

    def addProducer(self, producer):
        """
        Adds a producer to this DROP.

        Producers are AppDROPs that write into this DROP; from the
        producers' point of view, this DROP is one of its many outputs.

        When a producer has finished its execution, this DROP will be
        notified via the self.producerFinished() method.
        """

        # Don't add twice
        if producer in self._producers:
            return

        self._producers.append(producer)

        # Automatic back-reference
        if hasattr(producer, 'addOutput'):
            producer.addOutput(self)

    def producerFinished(self, uid):
        """
        Callback called by each of the producers of this DROP when their
        execution finishes. Once all producers have finished this DROP
        moves to the COMPLETED state.

        This is one of the key mechanisms through which the execution of a
        DROP graph is accomplished. If AppDROP A produces DROP
        B, as soon as A finishes its execution B will be notified and will move
        itself to COMPLETED.
        """

        # Is the UID actually referencing a producer
        if uid not in [p.uid for p in self._producers]:
            raise Exception("%s is not a producer of %r" % (uid, self))

        with self._finishedProducersLock:
            nFinished = len(self._finishedProducers)
            if nFinished >= len(self._producers):
                raise Exception("More producers finished that registered in DROP %r" % (self))
            self._finishedProducers.add(uid)

        if (nFinished+1) == len(self._producers):
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("All producers finished for DROP %r, proceeding to COMPLETE" % (self))
            self.setCompleted()

    @property
    def streamingConsumers(self):
        """
        The list of 'streaming' consumers held by this DROP.

        :see: `self.addStreamingConsumer()`
        """
        return self._streamingConsumers[:]

    def addStreamingConsumer(self, streamingConsumer):
        """
        Adds a streaming consumer to this DROP.

        Streaming consumers are AppDROPs that receive the data written
        into this DROP *as it gets written*, and therefore do not need to
        wait until this DROP has been moved to the COMPLETED state.
        """

        # Consumers have a "consume" method that gets invoked when
        # this DROP moves to COMPLETED
        if not hasattr(streamingConsumer, 'dropCompleted') or not hasattr(streamingConsumer, 'dataWritten'):
            raise Exception("The streaming consumer %r doesn't have a 'dropCompleted' and/or 'dataWritten' method" % (streamingConsumer))

        # An object cannot be a normal and streaming streamingConsumer at the same time,
        # see the comment in the __init__ method
        if streamingConsumer in self._consumers:
            raise Exception("Consumer %s is already registered as a normal consumer" % (streamingConsumer))

        # Add if not already present
        if streamingConsumer in self._streamingConsumers:
            return
        logger.debug('Adding new streaming streaming consumer for DROP %s/%s: %s' %(self.oid, self.uid, streamingConsumer))
        self._streamingConsumers.append(streamingConsumer)

        # Automatic back-reference
        if hasattr(streamingConsumer, 'addStreamingInput'):
            streamingConsumer.addStreamingInput(self)

    def setCompleted(self):
        '''
        Moves this DROP to the COMPLETED state. This can be used when not all the
        expected data has arrived for a given DROP, but it should still be moved
        to COMPLETED, or when the expected amount of data held by a DROP
        is not known in advanced.
        '''
        if self.status not in [DROPStates.INITIALIZED, DROPStates.WRITING]:
            raise Exception("DROP %s/%s not in INITIALIZED or WRITING state (%s), cannot setComplete()" % (self._oid, self._uid, self.status))

        # Close our writing IO instance.
        # If written externally, self._wio will have remained None
        if self._wio:
            self._wio.close()

        if logger.isEnabledFor(logging.INFO):
            logger.info("Moving DROP %s/%s to COMPLETED" % (self._oid, self._uid))
        self.status = DROPStates.COMPLETED

        # Signal our streaming consumers that the show is over
        for ic in self._streamingConsumers:
            ic.dropCompleted(self.uid)

    def isCompleted(self):
        '''
        Checks whether this DROP is currently in the COMPLETED state or not
        '''
        # Mind you we're not accessing _status, but status. This way we use the
        # lock in status() to access _status
        return (self.status == DROPStates.COMPLETED)

    @property
    def location(self):
        """
        An attribute indicating the physical location of this DROP. Its
        value doesn't necessarily represent the real physical location of the
        object or its data, and should simply be used as an informal piece of
        information
        """
        return self._location

    @location.setter
    def location(self, value):
        self._location = value

    @property
    def node(self):
        return self._node

    @property
    def uri(self):
        """
        An attribute indicating the URI of this DROP. The meaning of this
        URI is not formal, and it's currently used to hold the Pyro URI of
        DROPs that are activated via a Pyro Daemon.
        """
        return self._uri

    @uri.setter
    def uri(self, uri):
        self._uri = uri

class FileDROP(AbstractDROP):
    """
    A DROP that points to data stored in a mounted filesystem.
    """

    def initialize(self, **kwargs):
        """
        FileDROP-specific initialization.
        """
        self._root = self._getArg(kwargs, 'dirname', '/tmp/sdp_dfms')
        if (not os.path.exists(self._root)):
            os.mkdir(self._root)
        self._root = os.path.abspath(self._root)

        # TODO: Make sure the parts that make up the filename are composed
        #       of valid filename characters; otherwise encode them
        self._fnm = self._root + os.sep + self._oid + '___' + self.uid
        if os.path.isfile(self._fnm):
            logger.warn('File %s already exists, overwriting' % (self._fnm))

        self._wio = None

    def getIO(self):
        return FileIO(self._fnm)

    @property
    def path(self):
        """
        Returns the absolute path of the file pointed by this DROP.
        """
        return self._fnm

    @property
    def dataURL(self):
        hostname = os.uname()[1] # TODO: change when necessary
        return "file://" + hostname + self._fnm

class NgasDROP(AbstractDROP):
    '''
    A DROP that points to data stored in an NGAS server
    '''

    def initialize(self, **kwargs):
        self._ngasSrv            = self._getArg(kwargs, 'ngasSrv', 'localhost')
        self._ngasPort           = int(self._getArg(kwargs, 'ngasPort', 7777))
        self._ngasTimeout        = int(self._getArg(kwargs, 'ngasConnectTimeout', 2))
        self._ngasConnectTimeout = int(self._getArg(kwargs, 'ngasTimeout', 2))

    def getIO(self):
        return NgasIO(self._ngasSrv, self.uid, port=self._ngasPort,
                      ngasConnectTimeout=self._ngasConnectTimeout,
                      ngasTimeout=self._ngasTimeout)

    @property
    def dataURL(self):
        return "ngas://%s:%d/%s" % (self._ngasSrv, self._ngasPort, self.uid)

class InMemoryDROP(AbstractDROP):
    """
    A DROP that points data stored in memory.
    """

    def initialize(self, **kwargs):
        self._buf = StringIO()

    def getIO(self):
        return MemoryIO(self._buf)

    @property
    def dataURL(self):
        hostname = os.uname()[1]
        return "mem://%s/%d/%d" % (hostname, os.getpid(), id(self._buf))

class NullDROP(AbstractDROP):
    """
    A DROP that doesn't store any data.
    """

    def getIO(self):
        return NullIO()

    @property
    def dataURL(self):
        return "null://"

class ContainerDROP(AbstractDROP):
    """
    A DROP that doesn't directly point to some piece of data, but instead
    holds references to other DROPs (its children), and from them its own
    internal state is deduced.

    Because of its nature, ContainerDROPs cannot be written to directly,
    and likewise they cannot be read from directly. One instead has to pay
    attention to its "children" DROPs if I/O must be performed.
    """

    def initialize(self, **kwargs):
        super(ContainerDROP, self).initialize(**kwargs)
        self._children = []

    #===========================================================================
    # No data-related operations should actually be called in Container DROPs
    #===========================================================================
    def getIO(self):
        return ErrorIO()
    def dataURL(self):
        raise NotImplementedError()

    def addChild(self, child):

        # Avoid circular dependencies between Containers
        if child == self.parent:
            raise Exception("Circular dependency between DROPs %s/%s and %s/%s" % (self.oid, self.uid, child.oid, child.uid))

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Adding new child for ContainerDROP %s/%s: %s" % (self.oid, self.uid, child.uid))

        self._children.append(child)
        child.parent = self

    def delete(self):
        # TODO: this needs more thinking. Probably a separate method to perform
        #       this recursive deletion will be needed, while this delete method
        #       will go hand-to-hand with the rest of the I/O methods above,
        #       which are currently raise a NotImplementedError
        for c in [c for c in self._children if c.exists()]:
            c.delete()

    @property
    def expirationDate(self):
        if self._children:
            return heapq.nlargest(1, [c.expirationDate for c in self._children])[0]
        return -1

    @property
    def children(self):
        return self._children[:]

    def exists(self):
        if self._children:
            # TODO: Or should it be __and__? Depends on what the exact contract of
            #       "exists" is
            return reduce(lambda a,b: a or b, [c.exists() for c in self._children])
        return True

class DirectoryContainer(ContainerDROP):
    """
    A ContainerDROP that represents a filesystem directory. It only allows
    FileDROPs and DirectoryContainers to be added as children. Children
    can only be added if they are placed directly within the directory
    represented by this DirectoryContainer.
    """

    def initialize(self, **kwargs):
        ContainerDROP.initialize(self, **kwargs)

        if 'dirname' not in kwargs:
            raise Exception('DirectoryContainer needs a "dirname" parameter')

        directory = kwargs['dirname']

        check_exists = self._getArg(kwargs, 'check_exists', True)
        if check_exists is True:
            if not os.path.isdir(directory):
                raise Exception('%s is not a directory' % (directory))

        self._path = os.path.abspath(directory)

    def addChild(self, child):
        if isinstance(child, (FileDROP, DirectoryContainer)):
            path = child.path
            if os.path.dirname(path) != self.path:
                raise Exception('Child DROP is not under %s' % (self.path))
            ContainerDROP.addChild(self, child)
        else:
            raise TypeError('Child DROP is not of type FileDROP or DirectoryContainer')

    @property
    def path(self):
        return self._path


#===============================================================================
# AppDROP classes follow
#===============================================================================


class AppDROP(ContainerDROP):

    '''
    An AppDROP is a DROP representing an application that reads data
    from one or more DROPs (its inputs), and writes data onto one or more
    DROPs (its outputs).

    AppDROPs accept two different kind of inputs: "normal" and "streaming"
    inputs. Normal inputs are DROPs that must be on the COMPLETED state
    (and therefore their data must be fully written) before this application is
    run, while streaming inputs are DROPs that feed chunks of data into
    this application as the data gets written into them.

    This class contains two methods that should be overwritten as needed by
    subclasses: `dropCompleted`, invoked when input DROPs move to
    COMPLETED, and `dataWritten`, invoked with the data coming from streaming
    inputs.

    How and when applications are executed is completely up to the user, and is
    not enforced by this base class. Some applications might need to be run at
    `initialize` time, while other might start during the first invocation of
    `dataWritten`. A common scenario anyway is to start an application only
    after all its inputs have moved to COMPLETED (implying that none of them is
    an streaming input); for these cases see the `BarrierAppDROP`.
    '''

    def initialize(self, **kwargs):

        super(AppDROP, self).initialize(**kwargs)

        # Inputs and Outputs are the DROPs that get read from and written
        # to by this AppDROP, respectively. An input DROP will see
        # this AppDROP as one of its consumers, while an output DROP
        # will see this AppDROP as one of its producers.
        #
        # Input objects will 
        self._inputs  = collections.OrderedDict()
        self._outputs = collections.OrderedDict()

        # Same as above, only that these correspond to the 'streaming' version
        # of the consumers
        self._streamingInputs  = collections.OrderedDict()

        # An AppDROP has a second, separate state machine indicating its
        # execution status.
        self._execStatus = AppDROPStates.NOT_RUN

    def addInput(self, inputDrop):
        if inputDrop not in self._inputs.values():
            uid = inputDrop.uid
            self._inputs[uid] = inputDrop
            inputDrop.addConsumer(self)

    @property
    def inputs(self):
        """
        The list of inputs set into this AppDROP
        """
        return self._inputs.values()

    def addOutput(self, outputDrop):
        if outputDrop is self:
            raise Exception('Cannot add an AppConsumer as its own output')
        if outputDrop not in self._outputs.values():
            uid = outputDrop.uid
            self._outputs[uid] = outputDrop
            outputDrop.addProducer(self)

            def appFinished(e):
                if e.execStatus not in (AppDROPStates.FINISHED, AppDROPStates.ERROR):
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug('Skipping event for output %s: %s' %(outputDrop, str(e.__dict__)) )
                    return
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug('Notifying %r that its producer %r has finished: %s' %(outputDrop, self, str(e.__dict__)))
                outputDrop.producerFinished(self.uid)
            self.subscribe(appFinished, eventType='execStatus')

    @property
    def outputs(self):
        """
        The list of outputs set into this AppDROP
        """
        return self._outputs.values()

    def addStreamingInput(self, streamingInputDrop):
        if streamingInputDrop not in self._streamingInputs.values():
            uid = streamingInputDrop.uid
            self._streamingInputs[uid] = streamingInputDrop
            streamingInputDrop.addStreamingConsumer(self)

    @property
    def streamingInputs(self):
        """
        The list of streaming inputs set into this AppDROP
        """
        return self._streamingInputs.values()

    def dropCompleted(self, uid):
        """
        Callback invoked when the DROP with UID `uid` (which is either a
        normal or a streaming input of this AppDROP) has moved to the
        COMPLETED state. By default no action is performed.
        """

    def dataWritten(self, uid, data):
        """
        Callback invoked when `data` has been written into the DROP with
        UID `uid` (which is one of the streaming inputs of this AppDROP).
        By default no action is performed
        """

    @property
    def execStatus(self):
        """
        The execution status of this AppDROP
        """
        return self._execStatus

    @execStatus.setter
    def execStatus(self, execStatus):
        if self._execStatus == execStatus:
            return
        self._execStatus = execStatus
        self._fire('execStatus', execStatus=execStatus)

class BarrierAppDROP(AppDROP):
    """
    An AppDROP accepts no streaming inputs and waits until all its inputs
    have been moved to COMPLETED to execute its 'run' method, which must be
    overwritten by subclasses.

    In the case that this object has more than one input it effectively acts as
    a logical barrier that joins two threads of executions. In the case that
    this object has only one input this will act simply like a batch execution.
    """

    def initialize(self, **kwargs):
        super(BarrierAppDROP, self).initialize(**kwargs)
        self._completedInputs = []

    def addStreamingInput(self, streamingInputDrop):
        raise Exception("BarrierAppDROPs don't accept streaming inputs")

    def dropCompleted(self, uid):
        super(BarrierAppDROP, self).dropCompleted(uid)
        self._completedInputs.append(uid)
        if len(self._completedInputs) == len(self._inputs):
            # Return immediately, but schedule the execution of this app
            t = threading.Thread(None, lambda: self.execute())
            t.daemon = 1
            t.start()

    def execute(self):
        """
        Manually trigger the execution of this application.

        This method is normally invoked internally when the application detects
        all its inputs are COMPLETED.
        """

        # Keep track of the state of this application. Setting the state
        # will fire an event to the subscribers of the execStatus events
        self.execStatus = AppDROPStates.RUNNING
        try:
            self.run()
            self.execStatus = AppDROPStates.FINISHED
        except:
            self.execStatus = AppDROPStates.ERROR
            raise
        finally:
            # TODO: This needs to be defined more clearly
            self.status = DROPStates.COMPLETED

    def run(self):
        """
        Run this application. It can be safely assumed that at this point all
        the required inputs are COMPLETED.
        """

    # TODO: another thing we need to check
    def exists(self):
        return True

class dropdict(dict):
    """
    An intermediate representation of a DROP that can be easily serialized
    into a transport format such as JSON or XML.

    This dictionary holds all the important information needed to call any given
    DROP constructor. The most essential pieces of information are the
    DROP's OID, and its type (which determines the class to instantiate).
    Depending on the type more fields will be required. This class doesn't
    enforce these requirements though, as it only acts as an information
    container.

    This class also offers a few utility methods to make it look more like an
    actual DROP class. This way, users can use the same set of methods
    both to create DROPs representations (i.e., instances of this class)
    and actual DROP instances.

    Users of this class are, for example, the graph_loader module which deals
    with JSON -> DROP representation transformations, and the different
    repositories where graph templates are expected to be found by the
    DROPManager.
    """
    def _addSomething(self, other, key):
        if key not in self:
            self[key] = []
        self[key].append(other['oid'])

    def addConsumer(self, other):
        self._addSomething(other, 'consumers')
    def addStreamingConsumer(self, other):
        self._addSomething(other, 'streamingConsumers')
    def addInput(self, other):
        self._addSomething(other, 'inputs')
    def addStreamingInput(self, other):
        self._addSomething(other, 'streamingInputs')
    def addOutput(self, other):
        self._addSomething(other, 'outputs')
    def addProducer(self, other):
        self._addSomething(other, 'producers')
    def __setattr__(self, name, value):
        self[name] = value
    def __getattr__(self, name):
        return self[name]


# Dictionary mapping 1-to-many DROPLinkType constants to the corresponding methods
# used to append a a DROP into a relationship collection of another
# (e.g., one uses `addConsumer` to add a DROPLinkeType.CONSUMER DROP into
# another)
LINKTYPE_1TON_APPEND_METHOD = {
    DROPLinkType.CONSUMER:           'addConsumer',
    DROPLinkType.STREAMING_CONSUMER: 'addStreamingConsumer',
    DROPLinkType.INPUT:              'addInput',
    DROPLinkType.STREAMING_INPUT:    'addStreamingInput',
    DROPLinkType.OUTPUT:             'addOutput',
    DROPLinkType.CHILD:              'addChild',
    DROPLinkType.PRODUCER:           'addProducer'
}

# Same as above, but for N-to-1 relationships, in which case we indicate not a
# method but a property
LINKTYPE_NTO1_PROPERTY = {
    DROPLinkType.PARENT: 'parent'
}