"""C와 독립 픽셀 기준의 일치, 실제 호스트 연결, 빌드 무결성과 C 배열 경계."""
import json
from pathlib import Path
import random
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from test_painter_clip import reference_convert, ClipTests
import painter_native as native
import painter_usb as host


class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.directory.name) / native.DEFAULT.name
        native.build(cls.binary)
        cls.convert = staticmethod(native.load(cls.binary))

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_rgb_and_spatial_reference(self):
        # 기존 독립 기준 검증의 색 경계/위치/순서/캐시 격리 전체를 C에도 적용.
        import prepare_painter_clip as clip
        with patch.object(clip, 'convert', self.convert):
            result = unittest.TestResult()
            unittest.defaultTestLoader.loadTestsFromTestCase(ClipTests).run(result)
            self.assertEqual(result.errors, [])
            self.assertEqual(result.failures, [])

    def test_real_host_read(self):
        path = Path(self.directory.name) / 'frame.ppm'
        rgb = random.Random(716).randbytes(192000)
        path.write_bytes(b'P6\n# eink sequence 1\n320 200\n255\n' + rgb)
        calls = []
        def convert(value):
            calls.append(len(value))
            return self.convert(value)
        _, packed, info = host.read_frame(path, converter=convert)
        self.assertEqual(calls, [192000])
        self.assertEqual(packed, reference_convert(rgb))
        self.assertEqual(info['source_sequence'], 1)

    def test_stale_and_corrupt_build_rejected(self):
        manifest_path = self.binary.with_suffix(self.binary.suffix + '.json')
        original = manifest_path.read_text()
        try:
            for key in ('abi', 'source_sha256', 'binary_sha256'):
                manifest = json.loads(original)
                manifest[key] = 'wrong'
                manifest_path.write_text(json.dumps(manifest))
                with self.assertRaises(ValueError): native.load(self.binary)
        finally:
            manifest_path.write_text(original)
        with self.assertRaises(OSError): native.load(self.binary.with_name('missing'))

    def test_invalid_native_build_never_opens_serial(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / 'failed'
            with patch.object(native, 'load', side_effect=ValueError('빌드 불일치')), patch.object(host, 'open_serial') as serial:
                self.assertEqual(host.main(['--native-conversion','--frame-path','unused',
                                            '--port','unused','--log-prefix',str(prefix)]),1)
                serial.assert_not_called()
            result = json.loads(Path(str(prefix)+'-observation.json').read_text())
            self.assertEqual(result['status'], '오류')

    def test_c_bounds_and_invalid_buffers(self):
        code = r'''
#include <assert.h>
#include <string.h>
#include "painter_convert.c"
int main(void) {
  uint8_t input[192002],out[16002];
  memset(input,255,sizeof input); memset(out,0x6b,sizeof out);
  assert(painter_convert(input+1,192000,out+1,16000)==0);
  assert(out[0]==0x6b&&out[16001]==0x6b);
  for(int i=1;i<=16000;++i)assert(out[i]==0);
  size_t bad[]={0,1,15999,16001,191999,192001};
  for(int i=0;i<6;++i){
    memset(out,0x6b,sizeof out);
    assert(painter_convert(input+1,bad[i],out+1,16000)==-1);
    assert(painter_convert(input+1,192000,out+1,bad[i])==-1);
    for(int j=0;j<16002;++j)assert(out[j]==0x6b);
  }
  assert(painter_convert(0,192000,out+1,16000)==-1);
  assert(painter_convert(input+1,192000,0,16000)==-1);
}
'''
        path = Path(self.directory.name)
        (path/'check.c').write_text(code)
        subprocess.run(['cc','-std=c11','-fsanitize=address,undefined','-g',
                        '-I',str(native.SOURCE.parent),str(path/'check.c'),'-o',str(path/'check')],check=True)
        subprocess.run([str(path/'check')],check=True,timeout=10)


if __name__ == '__main__': unittest.main()
