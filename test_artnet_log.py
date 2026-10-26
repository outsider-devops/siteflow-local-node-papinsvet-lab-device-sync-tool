import json
from pathlib import Path
import tempfile
import unittest
from artnet_log import ArtNetLog


class LogTests(unittest.TestCase):
    def test_every_channel_survives_disk_and_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            log=ArtNetLog(tmp,min_free=0);log.start()
            values=[i%256 for i in range(512)]
            log.submit(dict(type='ArtDmx',sourceIp='192.168.18.80',universe=0,values=values))
            log.close()
            reopened=ArtNetLog(tmp,min_free=0)
            record=reopened.recent(1)[0]
            self.assertEqual(record['values'],values)
            self.assertEqual(record['values'][511],255)

    def test_rotation_is_bounded_and_tail_spans_segments(self):
        with tempfile.TemporaryDirectory() as tmp:
            log=ArtNetLog(tmp,max_bytes=100,segments=3,min_free=0)
            for n in range(20):log.write_record({'id':n,'data':'x'*25})
            self.assertLessEqual(len(list(Path(tmp).glob('*.jsonl'))),3)
            self.assertEqual(log.recent(1)[0]['id'],19)
            self.assertGreater(len(log.recent(20)),1)
            with self.assertRaises(ValueError):log.open_segment('../state.json')

    def test_full_record_not_just_first_changed_channels(self):
        with tempfile.TemporaryDirectory() as tmp:
            log=ArtNetLog(tmp,min_free=0)
            log.write_record({'type':'ArtDmx','values':[0]*511+[77]})
            fp,size=log.open_segment('artnet.jsonl')
            with fp:record=json.loads(fp.read(size))
            self.assertEqual(record['values'][-1],77)
            self.assertEqual(len(record['values']),512)


if __name__=='__main__':unittest.main(verbosity=2)
