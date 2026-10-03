"""명시적으로 빌드·검증한 동일 픽셀 C 변환기. 로드 실패시 대체 실행하지 않는다."""
import argparse
import ctypes
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

SOURCE = Path(__file__).with_name('painter_convert.c')
DEFAULT = SOURCE.parent.parent / '.local/native' / ('painter_convert.dylib' if sys.platform == 'darwin' else 'painter_convert.so')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build(output=DEFAULT):
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as directory:
        binary = Path(directory) / output.name
        flags = ['-dynamiclib'] if sys.platform == 'darwin' else ['-shared', '-fPIC']
        command = ['cc', '-std=c11', '-O2', *flags, str(SOURCE), '-o', str(binary)]
        subprocess.run(command, check=True)
        manifest = dict(abi=1, source_sha256=digest(SOURCE), binary_sha256=digest(binary), command=command)
        binary.replace(output)
        output.with_suffix(output.suffix + '.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    return manifest


def load(output=DEFAULT):
    output = Path(output).resolve()
    manifest = json.loads(output.with_suffix(output.suffix + '.json').read_text())
    if (not isinstance(manifest, dict) or manifest.get('abi') != 1
            or digest(SOURCE) != manifest.get('source_sha256')
            or digest(output) != manifest.get('binary_sha256')):
        raise ValueError('네이티브 변환기 원본/빌드 해시 불일치; 다시 빌드해야 합니다')
    library = ctypes.CDLL(str(output))
    library.painter_convert_abi.argtypes = []
    library.painter_convert_abi.restype = ctypes.c_int
    if library.painter_convert_abi() != 1:
        raise ValueError('네이티브 변환기 ABI 불일치')
    pointer = ctypes.POINTER(ctypes.c_uint8)
    library.painter_convert.argtypes = [pointer, ctypes.c_size_t, pointer, ctypes.c_size_t]
    library.painter_convert.restype = ctypes.c_int

    def convert(rgb):
        rgb = bytes(rgb)
        if len(rgb) != 192000:
            raise ValueError('RGB 길이는 320×200×3이어야 합니다')
        source = (ctypes.c_uint8 * 192000).from_buffer_copy(rgb)
        result = (ctypes.c_uint8 * 16000)()
        if library.painter_convert(source, len(source), result, len(result)) != 0:
            raise ValueError('네이티브 변환기 실패')
        return bytes(result)
    return convert


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=DEFAULT)
    args = parser.parse_args()
    result = build(args.output)
    load(args.output)
    print('동일 픽셀 C 변환기 빌드·해시·ABI 확인: ' + result['binary_sha256'])
