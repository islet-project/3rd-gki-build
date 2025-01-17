#!/usr/bin/python3
#
# Copyright 2015 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import errno
import random
from socket import *  # pylint: disable=wildcard-import
import time
import unittest

from scapy import all as scapy

import csocket
import multinetwork_base
import net_test


RTMGRP_NEIGH = 4

NUD_INCOMPLETE = 0x01
NUD_REACHABLE = 0x02
NUD_STALE = 0x04
NUD_DELAY = 0x08
NUD_PROBE = 0x10
NUD_FAILED = 0x20
NUD_PERMANENT = 0x80


# TODO: Support IPv4.
class NeighbourTest(multinetwork_base.MultiNetworkBaseTest):

  # Set a 500-ms retrans timer so we can test for ND retransmits without
  # waiting too long. Apparently this cannot go below 500ms.
  RETRANS_TIME_MS = 500

  # This can only be in seconds, so 1000 is the minimum.
  DELAY_TIME_MS = 1000

  # Unfortunately, this must be above the delay timer or the kernel ND code will
  # not behave correctly (e.g., go straight from REACHABLE into DELAY). This is
  # is fuzzed by the kernel from 0.5x to 1.5x of its value, so we need a value
  # that's 2x the delay timer.
  BASE_REACHABLE_TIME_MS = 2 * DELAY_TIME_MS
  MAX_REACHABLE_TIME_MS = 1.5 * BASE_REACHABLE_TIME_MS

  # Kernel default unicast solicit is 3, but it need be changed larger
  # when test recofiguration during probing
  UCAST_SOLICIT_DEFAULT = 3
  UCAST_SOLICIT_LARGE = 10

  @classmethod
  def setUpClass(cls):
    super(NeighbourTest, cls).setUpClass()
    for netid in cls.tuns:
      iface = cls.GetInterfaceName(netid)
      # This can't be set in an RA.
      for proto in ["ipv4", "ipv6"]:
          cls.SetSysctl(
              "/proc/sys/net/%s/neigh/%s/delay_first_probe_time" % (proto, iface),
              cls.DELAY_TIME_MS // 1000)
          cls.SetSysctl(
              "/proc/sys/net/%s/neigh/%s/retrans_time_ms" % (proto, iface),
              cls.RETRANS_TIME_MS)

  def setUp(self):
    super(NeighbourTest, self).setUp()

    for netid in self.tuns:
      # Clear the ND cache entries for all routers, so each test starts with
      # the IPv6 default router in state STALE.
      addr = self._RouterAddress(netid, 6)
      ifindex = self.ifindices[netid]
      self.iproute.UpdateNeighbour(6, addr, None, ifindex, NUD_FAILED)

      # Configure IPv6 by sending an RA.
      self.SendRA(netid,
                  retranstimer=self.RETRANS_TIME_MS,
                  reachabletime=self.BASE_REACHABLE_TIME_MS)

    self.sock = socket(AF_NETLINK, SOCK_RAW, NETLINK_ROUTE)
    self.sock.bind((0, RTMGRP_NEIGH))
    net_test.SetNonBlocking(self.sock)

    self.netid = random.choice(list(self.tuns.keys()))
    self.ifindex = self.ifindices[self.netid]

    # MultinetworkBaseTest always uses NUD_PERMANENT for router ARP entries.
    # Temporarily change those entries to NUD_STALE so we can test them.
    self.ChangeRouterNudState(4, NUD_STALE)

  def SetUnicastSolicit(self, proto, iface, value):
    self.SetSysctl(
        "/proc/sys/net/%s/neigh/%s/ucast_solicit" % (proto, iface), value)

  def tearDown(self):
    super(NeighbourTest, self).tearDown()
    # It is already reset to default by TearDownClass,
    # but here we need to set it to default after each testcase.
    iface = self.GetInterfaceName(self.netid)
    for proto in ["ipv4", "ipv6"]:
      self.SetUnicastSolicit(proto, iface, self.UCAST_SOLICIT_DEFAULT)

    # Change router ARP entries back to NUD_PERMANENT,
    # so as not to affect other tests.
    self.ChangeRouterNudState(4, NUD_PERMANENT)

  def ChangeRouterNudState(self, version, state):
    router = self._RouterAddress(self.netid, version)
    macaddr = self.RouterMacAddress(self.netid)
    self.iproute.UpdateNeighbour(version, router, macaddr, self.ifindex, state)
    self.ExpectNeighbourNotification(router, state)
    self.assertNeighbourState(state, router)

  def GetNeighbour(self, addr, ifindex):
    version = csocket.AddressVersion(addr)
    for msg, args in self.iproute.DumpNeighbours(version, ifindex):
      if args["NDA_DST"] == addr:
        return msg, args

  def GetNdEntry(self, addr):
    return self.GetNeighbour(addr, self.ifindex)

  def CheckNoNdEvents(self):
    self.assertRaisesErrno(errno.EAGAIN, self.sock.recvfrom, 4096, MSG_PEEK)

  def assertNeighbourState(self, state, addr):
    self.assertEqual(state, self.GetNdEntry(addr)[0].state)

  def assertNeighbourAttr(self, addr, name, value):
    self.assertEqual(value, self.GetNdEntry(addr)[1][name])

  def ExpectNeighbourNotification(self, addr, state, attrs=None):
    msg = self.sock.recv(4096)
    msg, actual_attrs = self.iproute.ParseNeighbourMessage(msg)
    self.assertEqual(addr, actual_attrs["NDA_DST"])
    self.assertEqual(state, msg.state)
    if attrs:
      for name in attrs:
        self.assertEqual(attrs[name], actual_attrs[name])

  def ExpectProbe(self, is_unicast, addr):
    version = csocket.AddressVersion(addr)
    llsrc = self.MyMacAddress(self.netid)
    if version == 6:
      if is_unicast:
        src = self.MyLinkLocalAddress(self.netid)
        dst = addr
      else:
        solicited = inet_pton(AF_INET6, addr)
        last3bytes = tuple([net_test.ByteToHex(b) for b in solicited[-3:]])
        dst = "ff02::1:ff%s:%s%s" % last3bytes
        src = self.MyAddress(6, self.netid)
      expected = (
          scapy.IPv6(src=src, dst=dst) /
          scapy.ICMPv6ND_NS(tgt=addr) /
          scapy.ICMPv6NDOptSrcLLAddr(lladdr=llsrc)
      )
      msg = "%s probe" % ("Unicast" if is_unicast else "Multicast")
      self.ExpectPacketOn(self.netid, msg, expected)
    else:  # version == 4
      if is_unicast:
        src = self._MyIPv4Address(self.netid)
        dst = addr
      else:
        raise NotImplementedError("This test does not support broadcast ARP")
      expected = scapy.ARP(psrc=src, pdst=dst, hwsrc=llsrc, op=1)
      msg = "Unicast ARP probe"
      self.ExpectPacketOn(self.netid, msg, expected)

  def ExpectUnicastProbe(self, addr):
    self.ExpectProbe(True, addr)

  def ExpectMulticastNS(self, addr):
    self.ExpectProbe(False, addr)

  def ReceiveUnicastAdvertisement(self, addr, mac, srcaddr=None, dstaddr=None,
                                  S=1, O=0, R=1):
    version = csocket.AddressVersion(addr)
    if srcaddr is None:
      srcaddr = addr
    if dstaddr is None:
      dstaddr = self.MyLinkLocalAddress(self.netid)
    if version == 6:
      packet = (
          scapy.Ether(src=mac, dst=self.MyMacAddress(self.netid)) /
          scapy.IPv6(src=srcaddr, dst=dstaddr) /
          scapy.ICMPv6ND_NA(tgt=addr, S=S, O=O, R=R) /
          scapy.ICMPv6NDOptDstLLAddr(lladdr=mac)
      )
      self.ReceiveEtherPacketOn(self.netid, packet)
    else:
      raise NotImplementedError

  def SendDnsRequest(self, addr):
    version = csocket.AddressVersion(addr)
    routing_mode = random.choice(["mark", "oif", "uid"])
    s = self.BuildSocket(version, net_test.UDPSocket, self.netid, routing_mode)
    s.connect((addr, 53))
    s.send(net_test.UDP_PAYLOAD)
    return s

  def MonitorSleepMs(self, interval, addr):
    slept = 0
    while slept < interval:
      sleep_ms = min(100, interval - slept)
      time.sleep(sleep_ms / 1000.0)
      slept += sleep_ms
      print(self.GetNdEntry(addr))

  def MonitorSleep(self, intervalseconds, addr):
    self.MonitorSleepMs(intervalseconds * 1000, addr)

  def SleepMs(self, ms):
    time.sleep(ms / 1000.0)

  def testNotifications(self):
    """Tests neighbour notifications.

    Relevant kernel commits:
      upstream net-next:
        765c9c6 neigh: Better handling of transition to NUD_PROBE state
        53385d2 neigh: Netlink notification for administrative NUD state change
          (only checked on kernel v3.13+, not on v3.10)

      android-3.10:
        e4a6d6b neigh: Better handling of transition to NUD_PROBE state

      android-3.18:
        2011e72 neigh: Better handling of transition to NUD_PROBE state
    """
    router4 = self._RouterAddress(self.netid, 4)
    router6 = self._RouterAddress(self.netid, 6)
    self.assertNeighbourState(NUD_STALE, router4)
    self.assertNeighbourState(NUD_STALE, router6)

    # Send a packet and check that we go into DELAY.
    s = self.SendDnsRequest(net_test.IPV6_ADDR)
    self.assertNeighbourState(NUD_DELAY, router6)

    # Wait for the probe interval, then check that we're in PROBE, and that the
    # kernel has notified us.
    self.SleepMs(self.DELAY_TIME_MS * 1.1)
    self.ExpectNeighbourNotification(router6, NUD_PROBE)
    self.assertNeighbourState(NUD_PROBE, router6)
    self.ExpectUnicastProbe(router6)

    # Respond to the NS and verify we're in REACHABLE again.
    self.ReceiveUnicastAdvertisement(router6, self.RouterMacAddress(self.netid))
    self.assertNeighbourState(NUD_REACHABLE, router6)
    self.ExpectNeighbourNotification(router6, NUD_REACHABLE)

    # Wait until the reachable time has passed, and verify we're in STALE.
    self.SleepMs(self.MAX_REACHABLE_TIME_MS * 1.2)
    self.assertNeighbourState(NUD_STALE, router6)
    self.ExpectNeighbourNotification(router6, NUD_STALE)

    # Send a packet, and verify we go into DELAY and then to PROBE.
    s.send(net_test.UDP_PAYLOAD)
    self.assertNeighbourState(NUD_DELAY, router6)
    self.SleepMs(self.DELAY_TIME_MS * 1.1)
    self.assertNeighbourState(NUD_PROBE, router6)
    self.ExpectNeighbourNotification(router6, NUD_PROBE)

    # Wait for the probes to time out, and expect a FAILED notification.
    self.assertNeighbourAttr(router6, "NDA_PROBES", 1)
    self.ExpectUnicastProbe(router6)

    self.SleepMs(self.RETRANS_TIME_MS)
    self.ExpectUnicastProbe(router6)
    self.assertNeighbourAttr(router6, "NDA_PROBES", 2)

    self.SleepMs(self.RETRANS_TIME_MS)
    self.ExpectUnicastProbe(router6)
    self.assertNeighbourAttr(router6, "NDA_PROBES", 3)

    self.SleepMs(self.RETRANS_TIME_MS)
    self.assertNeighbourState(NUD_FAILED, router6)
    self.ExpectNeighbourNotification(router6, NUD_FAILED, {"NDA_PROBES": 3})

  def testRepeatedProbes(self):
    router4 = self._RouterAddress(self.netid, 4)
    router6 = self._RouterAddress(self.netid, 6)
    routermac = self.RouterMacAddress(self.netid)
    self.assertNeighbourState(NUD_STALE, router4)
    self.assertNeighbourState(NUD_STALE, router6)

    def ForceProbe(addr, mac):
      self.iproute.UpdateNeighbour(6, addr, None, self.ifindex, NUD_PROBE)
      self.assertNeighbourState(NUD_PROBE, addr)
      self.ExpectNeighbourNotification(addr, NUD_PROBE)
      self.SleepMs(1)  # TODO: Why is this necessary?
      self.assertNeighbourState(NUD_PROBE, addr)
      self.ExpectUnicastProbe(addr)
      self.ReceiveUnicastAdvertisement(addr, mac)
      self.assertNeighbourState(NUD_REACHABLE, addr)
      self.ExpectNeighbourNotification(addr, NUD_REACHABLE)

    for _ in range(5):
      ForceProbe(router6, routermac)

  def testIsRouterFlag(self):
    router6 = self._RouterAddress(self.netid, 6)
    self.assertNeighbourState(NUD_STALE, router6)

    # Get into FAILED.
    ifindex = self.ifindices[self.netid]
    self.iproute.UpdateNeighbour(6, router6, None, ifindex, NUD_FAILED)
    self.ExpectNeighbourNotification(router6, NUD_FAILED)
    self.assertNeighbourState(NUD_FAILED, router6)

    time.sleep(1)

    # Send another packet and expect a multicast NS.
    self.SendDnsRequest(net_test.IPV6_ADDR)
    self.ExpectMulticastNS(router6)

    # Receive a unicast NA with the R flag set to 0.
    self.ReceiveUnicastAdvertisement(router6, self.RouterMacAddress(self.netid),
                                     srcaddr=self._RouterAddress(self.netid, 6),
                                     dstaddr=self.MyAddress(6, self.netid),
                                     S=1, O=0, R=0)

    # Expect that this takes us to REACHABLE.
    self.ExpectNeighbourNotification(router6, NUD_REACHABLE)
    self.assertNeighbourState(NUD_REACHABLE, router6)

  def DoReconfigureDuringProbing(self, version):
    if version == 6:
      proto = "ipv6"
      ip_addr = net_test.IPV6_ADDR
    else:
      proto = "ipv4"
      ip_addr = net_test.IPV4_ADDR
    router = self._RouterAddress(self.netid, version)
    self.assertNeighbourState(NUD_STALE, router)

    iface = self.GetInterfaceName(self.netid)
    # set unicast solicit larger.
    self.SetUnicastSolicit(proto, iface, self.UCAST_SOLICIT_LARGE)

    # Send a packet and check that we go into DELAY.
    self.SendDnsRequest(ip_addr)
    self.assertNeighbourState(NUD_DELAY, router)

    # Probing 4 times but no reponse
    self.SleepMs(self.DELAY_TIME_MS * 1.1)
    self.ExpectNeighbourNotification(router, NUD_PROBE)
    self.assertNeighbourState(NUD_PROBE, router)
    self.ExpectUnicastProbe(router)

    for i in range(0, 3):
      self.SleepMs(self.RETRANS_TIME_MS)
      self.ExpectUnicastProbe(router)

    # reconfiguration to 3 while probing and the state change to NUD_FAILED
    self.SetUnicastSolicit(proto, iface, self.UCAST_SOLICIT_DEFAULT)
    self.SleepMs(self.RETRANS_TIME_MS)
    self.ExpectNeighbourNotification(router, NUD_FAILED)
    self.assertNeighbourState(NUD_FAILED, router)

  # Check neighbor state after re-config ARP probe times.
  def testReconfigureDuringProbing(self):
    self.DoReconfigureDuringProbing(4)
    self.DoReconfigureDuringProbing(6)

if __name__ == "__main__":
  unittest.main()
