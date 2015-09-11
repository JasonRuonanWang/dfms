#
#    ICRAR - International Centre for Radio Astronomy Research
#    (c) UWA - The University of Western Australia, 2015
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
"""
A small module that measures the average memory consumption of different
DataObject types. It was initially developed to address PRO-234.
"""

from optparse import OptionParser
import sys

import psutil

from dfms import data_object


def measure(n, DOtype):
    """
    Create `n` DataObjects of type `DOtype` and measure how much memory does the
    program use at the beginning and the end of the process. It returns a list
    with the total amount of memory, user time and system time used during the
    creation of all the DataObject instances
    """
    p = psutil.Process()
    mem1 = p.memory_info()[0]
    uTime1, sTime1 = p.cpu_times()
    dos = []
    for i in xrange(n):
        uid = str(i)
        dos.append(DOtype(uid, uid))
    mem2 = p.memory_info()[0]
    uTime2, sTime2 = p.cpu_times()

    return mem2 - mem1, uTime2 - uTime1, sTime2 - sTime1

if __name__ == '__main__':

    parser = OptionParser()
    parser.add_option("--csv", action="store_true", dest="csv", help = "Output results in CSV format", default=False)
    parser.add_option("-i", "--instances", action="store", type="int",
                      dest="instances", help = "Number of DataObject instances to create and measure")
    parser.add_option("-t", "--type", action="store", type="string",
                      dest="type", help = "DataObject type to instantiate")
    (options, args) = parser.parse_args(sys.argv)

    if options.type is None:
        parser.error("DataObject type to instantiate not specified")
    if options.instances is None:
        parser.error("Number of instances to create not specified")

    n = options.instances
    dotype = getattr(data_object, options.type)
    mem, uTime, sTime = measure(n, dotype)
    tTime = uTime + sTime
    memAvg, uTimeAvg, sTimeAvg, tTimeAvg = [x/float(n) for x in mem, uTime, sTime, tTime]

    if options.csv:
        print "%s,%d,%d,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f" % (options.type, n, mem, uTime*1e3, sTime*1e3, tTime*1e3, memAvg, uTimeAvg*1e6, sTimeAvg*1e6, tTimeAvg*1e6)
    else:
        print "%d bytes used by %d %ss (%.2f bytes per DO)" % (mem, n, dotype.__name__, memAvg)
        print "Total time:  %.2f msec (%.2f msec per DO)" % (tTime, tTimeAvg)
        print "User time:   %.2f msec (%.2f msec per DO)" % (uTime, uTimeAvg)
        print "System time: %.2f msec (%.2f msec per DO)" % (sTime, sTimeAvg)