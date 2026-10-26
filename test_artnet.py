"""Art-Net wire decoding and actual loopback receive tests. No fixture traffic."""
import socket
import struct
import time
import unittest
from unittest.mock import patch
from artnet_monitor import ArtNetMonitor, parse_packet, ARTPOLL, ARTNET_ID, receiver_reply


def dmx(universe=0, values=b'\x00\xff', sequence=1, physical=0):
    return ARTNET_ID + struct.pack('<H',0x5000) + b'\x00\x0e' + bytes([sequence,physical,universe & 255,universe >> 8]) + struct.pack('>H',len(values)) + values


class ArtNetTests(unittest.TestCase):
    def test_universe_byte_order_and_physical_are_separate(self):
        p=parse_packet(dmx(0x1234,physical=7))
        self.assertEqual((p['universe'],p['net'],p['subnet'],p['universeNibble'],p['physical']),(4660,18,3,4,7))
        self.assertEqual(p['values'],[0,255])

    def test_truncation_invalid_length_and_version(self):
        for packet in [b'',dmx()[:-1],dmx(values=b'\x01'),dmx(values=bytes(514)),dmx()[:10]+b'\x00\x01'+dmx()[12:]]:
            with self.assertRaises(ValueError):parse_packet(packet)

    def test_two_sources_not_merged_and_zeroes_retained(self):
        m=ArtNetMonitor()
        m.ingest(dmx(1,b'\x00\x05'),('192.168.18.80',6454))
        m.ingest(dmx(1,b'\x63\x00'),('192.168.18.90',6454))
        streams=m.snapshot()['streams']
        self.assertEqual(len(streams),2)
        self.assertEqual(streams[0]['values'],[0,5])
        self.assertEqual(streams[1]['values'],[99,0])

    def test_changes_and_sequence_wrap(self):
        m=ArtNetMonitor();m.ingest(dmx(sequence=255),('192.168.18.80',6454))
        m.ingest(dmx(values=b'\x02\xff',sequence=1),('192.168.18.80',6454))
        s=m.snapshot()['streams'][0]
        self.assertEqual(s['changedChannels'],[1]);self.assertEqual(s['sequenceGaps'],0)
        self.assertEqual(m.snapshot()['events'][0]['changes'],[{'channel':1,'value':2}])

    def test_stale_values_never_remain_live(self):
        m=ArtNetMonitor();m.ingest(dmx(),('192.168.18.80',6454))
        with patch('artnet_monitor.time.monotonic',return_value=time.monotonic()+16):
            s=m.snapshot()['streams'][0]
        self.assertFalse(s['live']);self.assertEqual(s['values'],[0,255])

    def test_foreign_subnet_ignored_and_memory_bounded(self):
        m=ArtNetMonitor();m.ingest(dmx(),('100.90.183.2',6454))
        self.assertEqual(len(m.snapshot()['streams']),0)
        for u in range(80):m.ingest(dmx(u),('192.168.18.80',6454))
        self.assertEqual(len(m.snapshot()['streams']),64)

    def test_pollreply_reports_ports_not_assumed_universe_numbers(self):
        b=bytearray(239);b[:10]=ARTNET_ID+struct.pack('<H',0x2100)
        b[172:174]=b'\x00\x02';b[174]=128;b[175]=64;b[190]=5;b[187]=9
        p=parse_packet(bytes(b));self.assertTrue(p['ports'][1]['input'])
        self.assertFalse(p['ports'][1]['output']);self.assertEqual(p['ports'][1]['inputUniverse'],9)
        self.assertEqual(p['ports'][0]['outputUniverse'],5)

    def test_actual_udp_receiver_and_exclusive_bind(self):
        m=ArtNetMonitor(subnet='127.0.0.0/8',host_ip='127.0.0.1',port=0)
        self.assertTrue(m.start())
        other=ArtNetMonitor(port=m.port)
        self.assertFalse(other.start())
        try:
            with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as s:s.sendto(dmx(23),('127.0.0.1',m.port))
            deadline=time.monotonic()+2
            while not m.snapshot()['streams'] and time.monotonic()<deadline:time.sleep(.01)
            self.assertEqual(m.snapshot()['streams'][0]['universe'],23)
        finally:m.close();other.close()

    def test_transmitted_packets_are_discovery_not_dmx(self):
        m=ArtNetMonitor();m.running=True
        with patch('socket.socket') as mock:
            m.socket=mock.return_value;m.poll('192.168.18.80')
            self.assertEqual(m.socket.sendto.call_args_list[0].args,(ARTPOLL,('192.168.18.80',6454)))
            self.assertEqual(len(m.socket.sendto.call_args_list),2)
            self.assertTrue(all(c.args[0][8:10] in (b'\x00\x20',b'\x00\x21') for c in m.socket.sendto.call_args_list))
        self.assertEqual(ARTPOLL[8:10],b'\x00\x20')

    def test_visualizer_advertises_subscriptions_not_a_physical_gateway(self):
        data=receiver_reply('192.168.18.6',[0,1])
        self.assertEqual(data[200],6)
        reply=parse_packet(data)
        self.assertEqual([p['outputUniverse'] for p in reply['ports']],[0,1])
        self.assertEqual(reply['shortName'],'SiteFlow RX')


if __name__=='__main__':unittest.main(verbosity=2)
