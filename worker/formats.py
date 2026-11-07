"""The format list: one id per format, whatever names and extensions the engines use for it.

The table covers the formats people actually look for, so they get good names and the right family.
Anything else an engine reports is added under its own extension when the catalog is built.
"""

from __future__ import annotations

import re

# Display order in the format picker.
FAMILIES = {
    "document": "Documents",
    "spreadsheet": "Spreadsheets",
    "slides": "Slides",
    "ebook": "Ebooks",
    "image": "Images",
    "raw": "Camera RAW",
    "vector": "Vector",
    "audio": "Audio",
    "video": "Video",
    "media": "Audio and video",
    "subtitle": "Subtitles",
    "archive": "Archives",
    "disk": "Disk images",
    "data": "Data",
    "model": "3D models",
    "cad": "CAD",
    "map": "Maps",
    "font": "Fonts",
    "mail": "Mail",
    "science": "Science",
    "medical": "Medical",
    "diagram": "Diagrams",
    "encoded": "Encoded",
    "other": "Other",
}

# family | id | name | extensions (first is the one written) | flags
_TABLE = """
document|pdf|PDF document|pdf|
document|docx|Word document|docx|
document|docm|Word macro document|docm|
document|dotx|Word template|dotx|
document|doc|Word 97-2003 document|doc|
document|dot|Word 97-2003 template|dot|
document|odt|OpenDocument text|odt|
document|ott|OpenDocument text template|ott|
document|fodt|Flat OpenDocument text|fodt|
document|rtf|Rich Text|rtf|
document|txt|Plain text|txt,text,log|lossy
document|md|Markdown|md,markdown,mkd|lossy
document|rst|reStructuredText|rst|lossy
document|adoc|AsciiDoc|adoc,asciidoc|lossy
document|org|Org mode|org|lossy
document|tex|LaTeX|tex,latex|lossy
document|html|Web page|html,htm,xhtml|lossy
document|dbk|DocBook|dbk,docbook|lossy
document|wiki|MediaWiki|wiki,mediawiki|lossy
document|textile|Textile|textile|lossy
document|pages|Apple Pages|pages|
document|wpd|WordPerfect|wpd|
document|wps|Microsoft Works|wps|
document|lwp|Lotus Word Pro|lwp|
document|abw|AbiWord|abw,zabw|
document|hwp|Hangul Word|hwp|
document|sxw|OpenOffice.org 1 text|sxw|
document|ps|PostScript|ps|
document|xps|XPS document|xps,oxps|
document|djvu|DjVu document|djvu,djv|
document|ipynb|Jupyter notebook|ipynb|lossy
document|man|Man page|man,1|lossy
document|typ|Typst|typ|lossy
spreadsheet|xlsx|Excel workbook|xlsx|
spreadsheet|xlsm|Excel macro workbook|xlsm|
spreadsheet|xlsb|Excel binary workbook|xlsb|
spreadsheet|xltx|Excel template|xltx|
spreadsheet|xls|Excel 97-2003 workbook|xls|
spreadsheet|ods|OpenDocument spreadsheet|ods|
spreadsheet|fods|Flat OpenDocument spreadsheet|fods|
spreadsheet|csv|Comma-separated values|csv|lossy
spreadsheet|tsv|Tab-separated values|tsv,tab|lossy
spreadsheet|numbers|Apple Numbers|numbers|
spreadsheet|dif|Data Interchange Format|dif|lossy
spreadsheet|slk|SYLK|slk,sylk|lossy
spreadsheet|dbf|dBase table|dbf|
spreadsheet|wk1|Lotus 1-2-3|wk1,wks,123|
spreadsheet|sxc|OpenOffice.org 1 spreadsheet|sxc|
slides|pptx|PowerPoint|pptx|
slides|pptm|PowerPoint macro|pptm|
slides|ppsx|PowerPoint show|ppsx|
slides|potx|PowerPoint template|potx|
slides|ppt|PowerPoint 97-2003|ppt,pps,pot|
slides|odp|OpenDocument slides|odp|
slides|fodp|Flat OpenDocument slides|fodp|
slides|key|Apple Keynote|key|
slides|sxi|OpenOffice.org 1 slides|sxi|
ebook|epub|EPUB ebook|epub|lossy
ebook|mobi|Kindle MOBI|mobi,prc|lossy
ebook|azw3|Kindle AZW3|azw3,azw|lossy
ebook|fb2|FictionBook|fb2|lossy
ebook|lit|Microsoft Reader|lit|lossy
ebook|lrf|Sony BBeB|lrf|lossy
ebook|pdb|Palm document|pdb|lossy
ebook|cbz|Comic book ZIP|cbz|
ebook|cbr|Comic book RAR|cbr|
ebook|cbc|Comic book collection|cbc|
ebook|chm|Compiled HTML Help|chm|lossy
ebook|snb|Shanda Bambook|snb|lossy
ebook|tcr|TCR ebook|tcr|lossy
ebook|txtz|Zipped text ebook|txtz|lossy
ebook|htmlz|Zipped HTML ebook|htmlz|lossy
ebook|oeb|Open eBook|oeb|lossy
ebook|pml|Palm Markup|pml|lossy
ebook|rb|Rocket eBook|rb|lossy
image|png|PNG image|png|
image|apng|Animated PNG|apng|
image|jpg|JPEG image|jpg,jpeg,jpe,jfif|lossy
image|webp|WebP image|webp|lossy
image|heic|HEIC photo|heic,heif,hif|lossy
image|avif|AVIF image|avif|lossy
image|jxl|JPEG XL image|jxl|
image|jp2|JPEG 2000 image|jp2,j2k,j2c,jpf,jpx|
image|gif|GIF image|gif|lossy
image|tiff|TIFF image|tiff,tif|
image|bmp|Bitmap image|bmp,dib|
image|ico|Windows icon|ico|lossy
image|icns|Apple icon|icns|lossy
image|psd|Photoshop image|psd,psb|
image|xcf|GIMP image|xcf|
image|tga|Targa image|tga|
image|exr|OpenEXR image|exr|
image|hdr|Radiance HDR image|hdr|
image|pcx|PCX image|pcx|
image|ppm|Portable pixmap|ppm,pnm|
image|pgm|Portable graymap|pgm|
image|pbm|Portable bitmap|pbm|
image|dds|DirectDraw Surface|dds|lossy
image|fits|FITS image|fits,fit,fts|
image|sgi|Silicon Graphics image|sgi,rgb|
image|xpm|X pixmap|xpm|
image|xbm|X bitmap|xbm|
image|wbmp|Wireless bitmap|wbmp|lossy
raw|cr2|Canon RAW|cr2|
raw|cr3|Canon RAW 3|cr3|
raw|crw|Canon CRW|crw|
raw|nef|Nikon RAW|nef,nrw|
raw|arw|Sony RAW|arw,srf,sr2|
raw|dng|Adobe Digital Negative|dng|
raw|orf|Olympus RAW|orf|
raw|raf|Fujifilm RAW|raf|
raw|rw2|Panasonic RAW|rw2|
raw|pef|Pentax RAW|pef|
raw|srw|Samsung RAW|srw|
raw|3fr|Hasselblad RAW|3fr|
raw|erf|Epson RAW|erf|
raw|mrw|Minolta RAW|mrw|
raw|kdc|Kodak RAW|kdc,dcr|
raw|x3f|Sigma RAW|x3f|
raw|iiq|Phase One RAW|iiq|
vector|svg|SVG vector|svg|
vector|svgz|Compressed SVG|svgz|
vector|eps|Encapsulated PostScript|eps,epsf,epsi|
vector|ai|Adobe Illustrator|ai|
vector|emf|Enhanced Metafile|emf|
vector|wmf|Windows Metafile|wmf|
vector|cdr|CorelDRAW|cdr|
vector|cmx|Corel Metafile|cmx|
vector|vsd|Visio drawing|vsd,vss,vst|
vector|vsdx|Visio drawing (XML)|vsdx,vsdm,vssx,vstx|
vector|pub|Microsoft Publisher|pub|
vector|odg|OpenDocument drawing|odg|
vector|fodg|Flat OpenDocument drawing|fodg|
vector|cgm|Computer Graphics Metafile|cgm|
vector|sxd|OpenOffice.org 1 drawing|sxd|
audio|mp3|MP3 audio|mp3|lossy
audio|flac|FLAC audio|flac|
audio|wav|WAV audio|wav,wave|
audio|ogg|Ogg Vorbis audio|ogg,oga|lossy
audio|opus|Opus audio|opus|lossy
audio|aac|AAC audio|aac,adts|lossy
audio|m4a|MPEG-4 audio|m4a,m4b|lossy
audio|wma|Windows Media Audio|wma|lossy
audio|aiff|AIFF audio|aiff,aif,aifc|
audio|alac|Apple Lossless|caf|
audio|ac3|Dolby AC-3|ac3|lossy
audio|dts|DTS audio|dts|lossy
audio|amr|AMR audio|amr|lossy
audio|ape|Monkey's Audio|ape|
audio|wv|WavPack|wv|
audio|mka|Matroska audio|mka|lossy
audio|au|Sun audio|au,snd|
audio|voc|Creative Voice|voc|
audio|mid|MIDI|mid,midi,kar|
audio|mod|Tracker module|mod|
audio|xm|FastTracker module|xm|
audio|it|Impulse Tracker module|it|
audio|s3m|Scream Tracker module|s3m|
video|mp4|MP4 video|mp4,m4v|lossy
video|mkv|Matroska video|mkv|lossy
video|webm|WebM video|webm|lossy
video|mov|QuickTime video|mov,qt|lossy
video|avi|AVI video|avi|lossy
video|wmv|Windows Media video|wmv,asf|lossy
video|flv|Flash video|flv|lossy
video|mpg|MPEG video|mpg,mpeg|lossy
video|ts|MPEG transport stream|ts,m2ts,mts|lossy
video|vob|DVD video|vob|lossy
video|ogv|Ogg video|ogv|lossy
video|3gp|3GPP video|3gp,3g2|lossy
video|rm|RealMedia|rm,rmvb|lossy
video|mxf|MXF video|mxf|lossy
video|swf|Flash animation|swf|lossy
subtitle|srt|SubRip subtitles|srt|lossy
subtitle|vtt|WebVTT subtitles|vtt|lossy
subtitle|ass|Advanced SubStation subtitles|ass|lossy
subtitle|ssa|SubStation Alpha subtitles|ssa|lossy
subtitle|sub|MicroDVD subtitles|sub|lossy
archive|zip|ZIP archive|zip|
archive|7z|7-Zip archive|7z|
archive|tar|Tar archive|tar|
archive|tar.gz|Gzipped tar|tar.gz,tgz|
archive|tar.bz2|Bzipped tar|tar.bz2,tbz2,tbz|
archive|tar.xz|XZ tar|tar.xz,txz|
archive|tar.zst|Zstandard tar|tar.zst,tzst|
archive|gz|Gzip file|gz|
archive|bz2|Bzip2 file|bz2|
archive|xz|XZ file|xz|
archive|zst|Zstandard file|zst|
archive|rar|RAR archive|rar|
archive|cab|Windows cabinet|cab|
archive|cpio|CPIO archive|cpio|
archive|lzh|LHA archive|lzh,lha|
archive|arj|ARJ archive|arj|
archive|deb|Debian package|deb|
archive|rpm|RPM package|rpm|
archive|msi|Windows installer|msi|
archive|exe|Windows installer (Inno Setup)|exe|
archive|jar|Java archive|jar,war,ear|
archive|apk|Android package|apk|
archive|wim|Windows image|wim|
archive|dmg|Apple disk image|dmg|
archive|xar|XAR archive|xar,pkg|
disk|iso|Disc image|iso|
disk|raw|Raw disk image|img,raw|
disk|qcow2|QEMU disk image|qcow2,qcow|
disk|vmdk|VMware disk image|vmdk|
disk|vdi|VirtualBox disk image|vdi|
disk|vhd|Virtual PC disk image|vhd|
disk|vhdx|Hyper-V disk image|vhdx|
disk|qed|QEMU enhanced disk image|qed|
disk|chd|MAME compressed disk|chd|
disk|squashfs|SquashFS image|squashfs,sfs,snap|
data|json|JSON|json|
data|jsonl|JSON Lines|jsonl,ndjson|
data|yaml|YAML|yaml,yml|
data|toml|TOML|toml|
data|xml|XML|xml|
data|ini|INI settings|ini,cfg|
data|plist|Apple property list|plist|
data|msgpack|MessagePack|msgpack,mpack|
data|cbor|CBOR|cbor|
data|parquet|Apache Parquet|parquet,pq|
data|feather|Apache Feather|feather,arrow|
data|orc|Apache ORC|orc|
data|sqlite|SQLite database|sqlite,sqlite3,db,db3|
data|mdb|Access database|mdb,accdb|
data|dta|Stata data|dta|
data|sas7bdat|SAS data|sas7bdat,xpt|
data|sav|SPSS data|sav,zsav|
data|hdf5|HDF5 data|h5,hdf5,he5|
data|nc|NetCDF data|nc,cdf|
data|cdl|NetCDF text|cdl|lossy
data|mat|MATLAB data|mat|
data|npy|NumPy array|npy|
model|stl|STL model|stl|lossy
model|obj|Wavefront OBJ|obj|lossy
model|glb|glTF binary|glb|lossy
model|gltf|glTF|gltf|lossy
model|ply|PLY model|ply|lossy
model|fbx|Autodesk FBX|fbx|lossy
model|dae|COLLADA|dae|lossy
model|3ds|3D Studio|3ds|lossy
model|3mf|3D Manufacturing|3mf|lossy
model|x3d|X3D|x3d|lossy
model|amf|Additive Manufacturing|amf|lossy
model|off|Object File Format|off|lossy
model|blend|Blender scene|blend|lossy
model|scad|OpenSCAD source|scad|lossy
cad|dwg|AutoCAD drawing|dwg|lossy
cad|dxf|CAD exchange file|dxf|lossy
cad|step|STEP model|step,stp|lossy
cad|iges|IGES model|iges,igs|lossy
map|geojson|GeoJSON|geojson|
map|shp|Shapefile|shp|
map|kml|Google Earth KML|kml|
map|kmz|Google Earth KMZ|kmz|
map|gpkg|GeoPackage|gpkg|
map|gml|GML|gml|
map|gpx|GPS Exchange|gpx|
font|ttf|TrueType font|ttf|
font|otf|OpenType font|otf|
font|woff|Web font (WOFF)|woff|
font|woff2|Web font (WOFF2)|woff2|
font|pfb|PostScript Type 1 font|pfb,pfa|
font|sfd|FontForge source|sfd|
font|bdf|Bitmap font|bdf|lossy
font|pcf|X11 bitmap font|pcf|lossy
font|ttx|Font as XML|ttx|
mail|eml|Email message|eml|
mail|msg|Outlook message|msg|
mail|pst|Outlook mailbox|pst,ost|
mail|mbox|Mailbox|mbox|
mail|tnef|Outlook attachment bundle|tnef|
science|pdbx|Protein Data Bank|ent|
science|fasta|FASTA sequence|fasta,fa|
science|sam|SAM alignment|sam|
science|bam|BAM alignment|bam|
science|mol2|Sybyl molecule|mol2|
science|sdf|Structure-data file|sdf,sd|
science|smi|SMILES|smi,smiles|lossy
science|cif|Crystallographic Information|cif|
science|xyz|XYZ coordinates|xyz|lossy
science|mol|MDL molecule|mol,mdl|
medical|dcm|DICOM image|dcm,dicom|
diagram|gv|Graphviz|gv,dot|
encoded|b64|Base64 text|b64,base64|
encoded|hex|Hex dump|hex|
encoded|strings|Readable strings||
encoded|metadata|File details (JSON)||
encoded|pdfocr|Searchable PDF (OCR)||
"""

# Formats you can ask for but that never appear as an input: what they write has its own name.
_OUT_EXT = {"strings": "txt", "metadata": "json", "pdfocr": "pdf"}


class FormatDB:
    def __init__(self):
        self.formats: dict[str, dict] = {}
        self.by_ext: dict[str, str] = {}
        for line in _TABLE.strip().splitlines():
            family, fid, name, exts, flags = line.split("|")
            ext_list = [e for e in exts.split(",") if e]
            self.formats[fid] = {
                "id": fid, "name": name, "family": family, "exts": ext_list,
                "lossy": flags == "lossy",
            }
            if fid in _OUT_EXT:
                self.formats[fid]["out_ext"] = _OUT_EXT[fid]
            for ext in ext_list:
                self.by_ext.setdefault(ext, fid)

    def resolve(self, ext: str) -> str | None:
        return self.by_ext.get(ext.lower().lstrip("."))

    def ensure(self, ext: str, name: str | None = None, family: str = "other", lossy: bool = False) -> str | None:
        """The format id for an extension, adding it if it's new. None for anything unusable."""
        ext = ext.lower().strip().lstrip(".")
        if not re.fullmatch(r"[a-z0-9][a-z0-9._+-]{0,31}", ext):
            return None
        found = self.by_ext.get(ext)
        if found:
            return found
        if ext in self.formats:
            return ext
        self.formats[ext] = {
            "id": ext, "name": (name or f"{ext.upper()} file")[:80], "family": family,
            "exts": [ext], "lossy": lossy,
        }
        self.by_ext[ext] = ext
        return ext

    def ids(self, exts: list[str]) -> list[str]:
        result = []
        for ext in exts:
            if ext in self.formats:
                result.append(ext)
                continue
            fid = self.ensure(ext)
            if fid:
                result.append(fid)
        return result

    def family(self, fid: str) -> str:
        return self.formats[fid]["family"]

    def export(self) -> list[dict]:
        return list(self.formats.values())
