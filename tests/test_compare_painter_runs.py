"""화질·입력이 다른 실측을 같은 성능 개선율로 섞지 않는지 확인한다."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'host'))
import compare_painter_runs as c
import test_analyze_painter_run as fixture


class Tests(unittest.TestCase):
    def make(self,root,name,quality='NORMAL',source_offset=0,mode='captured_replay',white_pair=False):
        path=root/name;path.mkdir()
        prefix,obs=fixture.Tests().fixture(path,step=.09)
        obs['quality']=quality;obs['input_mode']=mode;obs['white_pair']=white_pair
        for f in obs['frames']:f['source_sequence']+=source_offset
        Path(str(prefix)+'-observation.json').write_text(json.dumps(obs))
        return prefix

    def test_same_input_only(self):
        with tempfile.TemporaryDirectory() as d,patch.object(c,'ROOT',Path(d).resolve()):
            root=Path(d)
            prefixes=[self.make(root,'normal-a'),self.make(root,'normal-b'),
                      self.make(root,'fast',quality='FAST'),self.make(root,'other',source_offset=1),
                      self.make(root,'live',mode='latest_file'),self.make(root,'white',quality='FAST',white_pair=True),self.make(root,'white2',quality='FAST',white_pair=2)]
            groups=c.compare(prefixes)
            self.assertEqual([len(r) for r in groups.values()],[2,1,1,1,1,1])
            text=c.markdown(groups)
            self.assertIn('별도 모드',text);self.assertEqual(text.count('첫 실행 대비'),1)

    def test_corruption_refused(self):
        with tempfile.TemporaryDirectory() as d,patch.object(c,'ROOT',Path(d).resolve()):
            prefix=self.make(Path(d),'bad');path=Path(str(prefix)+'-tx.bin')
            raw=path.read_bytes();path.write_bytes(raw[:-1]+bytes([raw[-1]^1]))
            with self.assertRaises(ValueError):c.compare([prefix])

if __name__=='__main__':unittest.main()
