#!/usr/bin/env python3
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'host'))
import driver_budget as b

class BudgetTests(unittest.TestCase):
    def test_real_source_and_inset_counts(self):
        s=b.checked_source();r=b.budget(70,64,s)
        self.assertEqual(r['phases'],30);self.assertEqual(r['task_creations'],60)
        self.assertEqual(r['active_rows'],1920);self.assertEqual(r['dma_rows'],1980)
        self.assertEqual(r['fast_skips'],14220);self.assertEqual(r['rmt_total_calls'],16380)
        self.assertEqual(r['queue_send_calls'],1920);self.assertIsNone(r['time_prediction_us'])
        cold=b.budget(70,64,s,0)
        self.assertEqual(cold['dma_rows'],r['dma_rows']+2)
        self.assertEqual(cold['rmt_total_calls'],r['rmt_total_calls'])

    def test_full_height_flush(self):
        s,state=b.scan(0,540,30,0)
        self.assertEqual(s['active_rows'],540);self.assertEqual(s['dma_rows'],541)
        self.assertEqual(s['rmt_row_calls'],541);self.assertEqual(s['fast_skips'],0)
        self.assertEqual(s['row_pulse_ticks'],541*80);self.assertEqual(state,0)

    def test_skip_state_across_top_and_bottom_edges(self):
        top,state=b.scan(0,1,30,0)
        self.assertEqual(top['dma_rows'],3);self.assertEqual(state,539)
        bottom,state=b.scan(539,1,30,state)
        self.assertEqual(bottom['dma_rows'],2);self.assertEqual(bottom['fast_skips'],539)
        self.assertEqual(state,0)

    def test_invalid_geometry(self):
        for y,h in ((-1,1),(540,1),(0,0),(70,471)):
            with self.assertRaises(ValueError):b.scan(y,h,30,0)

    def test_unknown_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);(p/'epd_driver.c').write_text('changed')
            with self.assertRaisesRegex(ValueError,'소스'):b.checked_source(p)

    def test_long_region_exceeds_100ms_in_programmed_pulses_alone(self):
        r=b.budget(70,400,b.checked_source())
        self.assertGreater(r['rmt_programmed_pulse_us'],100000)
        self.assertEqual(r['caller_delay_requested_us'],170000)
        self.assertEqual(r['producer_delay_requested_us'],150000)

if __name__=='__main__':unittest.main(verbosity=2)
