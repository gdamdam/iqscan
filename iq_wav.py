"""Read two-channel PCM16 IQ payload boundaries in RIFF/RF64 WAV files."""
import struct
import warnings


def read_header(path):
    size=path.stat().st_size
    with path.open('rb') as f:
        header=f.read(12)
        if len(header)!=12 or header[:4] not in (b'RIFF',b'RF64') or header[8:]!=b'WAVE':
            raise ValueError('Not a supported RIFF/RF64 WAV file')
        fmt=None;data_size64=None
        while f.tell()+8<=size:
            tag,length=struct.unpack('<4sI',f.read(8));offset=f.tell()
            if tag==b'data' and length==0xffffffff:
                if data_size64 is None:raise ValueError('RF64 data chunk has no ds64 size')
                length=data_size64
            warning=None
            if offset+length>size:
                if tag!=b'data':raise ValueError('Truncated WAV header chunk')
                warning=f'WAV data size exceeds file by {offset+length-size} bytes; analyzing available complete IQ samples only. Ensure recording is stopped.'
                warnings.warn(warning)
                length=(size-offset)//4*4
            if tag==b'ds64':
                if length<28:raise ValueError('Invalid RF64 ds64 chunk')
                _,data_size64,_,_=struct.unpack('<QQQI',f.read(28))
            elif tag==b'fmt ':
                if length<16:raise ValueError('Invalid WAV format chunk')
                codec,channels,rate,byte_rate,align,bits=struct.unpack('<HHIIHH',f.read(16))
                if (codec,channels,bits,align)!=(1,2,16,4) or rate<=0 or byte_rate!=rate*4:
                    raise ValueError('IQ WAV requires two-channel 16-bit PCM (I then Q); other WAV encodings are not supported')
                fmt=rate
            elif tag==b'data':
                if fmt is None:raise ValueError('WAV format chunk must precede data')
                if not length or length%4:raise ValueError('WAV has empty or incomplete IQ samples')
                return dict(data_offset=offset,bytes=length,sample_rate=fmt,container=header[:4].decode(),warning=warning)
            f.seek(offset+length+(length%2))
    raise ValueError('WAV data chunk missing')
