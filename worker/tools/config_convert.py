"""Structured settings files between formats. Runs inside the sandbox.

Only safe loaders: yaml.safe_load (plain yaml.load can build arbitrary objects), defusedxml (ordinary
XML parsers can be made to read local files and expand entity bombs).
"""

import configparser
import json
import plistlib
import re
import sys
import tomllib
from datetime import date, datetime, time


def plain(value):
    """Reduce to what every format here can hold."""
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [plain(v) for v in value]
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def load(path, fmt):
    if fmt == "json":
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    if fmt == "yaml":
        import yaml
        with open(path, encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    if fmt == "toml":
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    if fmt == "xml":
        import xmltodict
        from defusedxml.ElementTree import parse
        from xml.etree.ElementTree import tostring
        return xmltodict.parse(tostring(parse(path).getroot(), encoding="unicode"))
    if fmt == "ini":
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(path, encoding="utf-8")
        return {section: dict(parser[section]) for section in parser.sections()}
    if fmt == "plist":
        with open(path, "rb") as fh:
            return plistlib.load(fh)
    if fmt == "msgpack":
        import msgpack
        with open(path, "rb") as fh:
            return msgpack.unpack(fh, raw=False, strict_map_key=False)
    if fmt == "cbor":
        import cbor2
        with open(path, "rb") as fh:
            return cbor2.load(fh)
    raise ValueError(fmt)


def strip_nulls(value):
    """TOML and plist have no null."""
    if isinstance(value, dict):
        return {k: strip_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [strip_nulls(v) for v in value if v is not None]
    return value


_NOT_IN_A_NAME = re.compile(r"[^A-Za-z0-9_.-]")


def xml_name(key) -> str:
    """Column names often start with a digit or hold spaces, which an XML element name can't."""
    key = str(key)
    if key.startswith(("@", "#")):
        return key  # xmltodict's markers for attributes and text
    name = _NOT_IN_A_NAME.sub("_", key) or "_"
    return name if re.match(r"[A-Za-z_]", name) and not name.lower().startswith("xml") else f"_{name}"


def xml_names(value):
    if isinstance(value, dict):
        return {xml_name(k): xml_names(v) for k, v in value.items()}
    if isinstance(value, list):
        return [xml_names(v) for v in value]
    return value


def save(data, path, fmt):
    if fmt == "json":
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
    elif fmt == "yaml":
        import yaml
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False)
    elif fmt == "toml":
        import tomli_w
        root = data if isinstance(data, dict) else {"data": data}
        with open(path, "wb") as fh:
            tomli_w.dump(strip_nulls(root), fh)
    elif fmt == "xml":
        import xmltodict
        data = xml_names(data)
        if isinstance(data, list):
            # A table is a list of rows, and XML needs a single root element around them.
            root = {"data": {"row": data}}
        elif isinstance(data, dict) and len(data) == 1 and not isinstance(next(iter(data.values())), list):
            root = data
        else:
            root = {"data": data}
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(xmltodict.unparse(root, pretty=True))
    elif fmt == "ini":
        parser = configparser.ConfigParser(interpolation=None)
        root = data if isinstance(data, dict) else {"data": {"value": json.dumps(data)}}
        for section, values in root.items():
            if isinstance(values, dict):
                parser[str(section)] = {str(k): json.dumps(v) if isinstance(v, (dict, list)) else str(v) for k, v in values.items()}
            else:
                parser.setdefault("DEFAULT", {})
                parser["DEFAULT"][str(section)] = json.dumps(values) if isinstance(values, (dict, list)) else str(values)
        with open(path, "w", encoding="utf-8") as fh:
            parser.write(fh)
    elif fmt == "plist":
        with open(path, "wb") as fh:
            plistlib.dump(strip_nulls(data if isinstance(data, dict) else {"data": data}), fh)
    elif fmt == "msgpack":
        import msgpack
        with open(path, "wb") as fh:
            msgpack.pack(data, fh)
    elif fmt == "cbor":
        import cbor2
        with open(path, "wb") as fh:
            cbor2.dump(data, fh)
    else:
        raise ValueError(fmt)


def main():
    source, output, src_fmt, dst_fmt = sys.argv[1:5]
    sys.setrecursionlimit(2000)
    save(plain(load(source, src_fmt)), output, dst_fmt)


main()
