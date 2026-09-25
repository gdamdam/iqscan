"""Read two-channel IQ payload boundaries in RIFF/RF64 WAV files."""
import struct
import warnings

# (codec, bits) -> iqscan format; codec 1 is PCM, 3 is IEEE float. WAVE_FORMAT_EXTENSIBLE
# carries the real codec in the first two bytes of its SubFormat GUID.
_CODECS={(1,8):'cu8',(1,16):'cs16',(3,32):'cf32_le'}
_WIDTH={'cu8':2,'cs16':4,'cf32_le':8}


def read_header(path):
    size=path.stat().st_size
    with path.open('rb') as f:
        header=f.read(12)
        if len(header)!=12 or header[:4] not in (b'RIFF',b'RF64') or header[8:]!=b'WAVE':
            raise ValueError('Not a supported RIFF/RF64 WAV file')
        fmt=None;data_size64=None;center=None
        while f.tell()+8<=size:
            tag,length=struct.unpack('<4sI',f.read(8));offset=f.tell()
            if tag==b'data' and length==0xffffffff:
                if data_size64 is None:raise ValueError('RF64 data chunk has no ds64 size')
                length=data_size64
            warning=None
            if offset+length>size:
                if tag!=b'data':raise ValueError('Truncated WAV header chunk')
                if fmt is None:raise ValueError('WAV format chunk must precede data')
                warning=f'WAV data size exceeds file by {offset+length-size} bytes; analyzing available complete IQ samples only. Ensure recording is stopped.'
                warnings.warn(warning)
                length=(size-offset)//fmt[1]*fmt[1]
            if tag==b'ds64':
                if length<28:raise ValueError('Invalid RF64 ds64 chunk')
                _,data_size64,_,_=struct.unpack('<QQQI',f.read(28))
            elif tag==b'fmt ':
                if length<16:raise ValueError('Invalid WAV format chunk')
                codec,channels,rate,byte_rate,align,bits=struct.unpack('<HHIIHH',f.read(16))
                if codec==0xfffe and length>=40:
                    codec=struct.unpack_from('<H',f.read(24),8)[0]
                kind=_CODECS.get((codec,bits))
                if channels!=2 or kind is None or align!=_WIDTH[kind] or rate<=0 or byte_rate!=rate*align:
                    raise ValueError('IQ WAV requires two-channel 8/16-bit PCM or 32-bit float (I then Q); other WAV encodings are not supported')
                fmt=(rate,align,kind)
            elif tag==b'auxi' and length>=36:
                # SDR#/HDSDR/SpectraVue auxiliary chunk: two SYSTEMTIMEs then centerFreq (uint32 Hz).
                center=struct.unpack_from('<I',f.read(36),32)[0] or None
            elif tag==b'data':
                if fmt is None:raise ValueError('WAV format chunk must precede data')
                if not length or length%fmt[1]:raise ValueError('WAV has empty or incomplete IQ samples')
                return dict(data_offset=offset,bytes=length,sample_rate=fmt[0],format=fmt[2],
                            center_frequency=center,container=header[:4].decode(),warning=warning)
            f.seek(offset+length+(length%2))
    raise ValueError('WAV data chunk missing')
