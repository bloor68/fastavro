#!/usr/bin/env python

import json
from os import SEEK_CUR
from struct import pack, unpack
from zlib import decompress
from cStringIO import StringIO

VERSION = 1
MAGIC = 'Obj' + chr(VERSION)
SYNC_SIZE = 16
META_SCHEMA = {
    'type' : 'record',
    'name' : 'org.apache.avro.file.Header',
    'fields' : [
       {
           'name': 'magic',
           'type': {'type': 'fixed', 'name': 'magic', 'size': len(MAGIC)},
       },
       {
           'name': 'meta',
           'type': {'type': 'map', 'values': 'bytes'}
       },
       {
           'name': 'sync',
           'type': {'type': 'fixed', 'name': 'sync', 'size': SYNC_SIZE}
       },
    ]
}

def read_null(fo, schema):
    '''null is written as zero bytes.'''
    return None

def read_boolean(fo, schema):
    '''A boolean is written as a single byte whose value is either 0 (false) or
    1 (true).
    '''
    return ord(fo.read(1)) == 1

def read_long(fo, schema):
    '''int and long values are written using variable-length, zig-zag coding.'''
    c = fo.read(1)
    if not c:
        raise EOFError

    b = ord(c)
    n = b & 0x7F
    shift = 7

    while (b & 0x80) != 0:
        b = ord(fo.read(1))
        n |= (b & 0x7F) << shift
        shift += 7

    return (n >> 1) ^ -(n & 1)

def read_float(fo, schema):
    '''A float is written as 4 bytes.

    The float is converted into a 32-bit integer using a method equivalent to
    Java's floatToIntBits and then encoded in little-endian format.
    '''
    bits = (((ord(fo.read(1)) & 0xffL)) |
            ((ord(fo.read(1)) & 0xffL) <<  8) |
            ((ord(fo.read(1)) & 0xffL) << 16) |
            ((ord(fo.read(1)) & 0xffL) << 24))

    return unpack('!f', pack('!I', bits))[0]

def read_double(fo, schema):
    '''A double is written as 8 bytes.

    The double is converted into a 64-bit integer using a method equivalent to
    Java's doubleToLongBits and then encoded in little-endian format.
    '''
    bits = (((ord(fo.read(1)) & 0xffL)) |
            ((ord(fo.read(1)) & 0xffL) <<  8) |
            ((ord(fo.read(1)) & 0xffL) << 16) |
            ((ord(fo.read(1)) & 0xffL) << 24) |
            ((ord(fo.read(1)) & 0xffL) << 32) |
            ((ord(fo.read(1)) & 0xffL) << 40) |
            ((ord(fo.read(1)) & 0xffL) << 48) |
            ((ord(fo.read(1)) & 0xffL) << 56))

    return unpack('!d', pack('!Q', bits))[0]

def read_bytes(fo, schema):
    '''Bytes are encoded as a long followed by that many bytes of data.'''
    return fo.read(read_long(fo, schema))

def read_utf8(fo, schema):
    '''A string is encoded as a long followed by that many bytes of UTF-8
    encoded character data.
    '''
    return unicode(read_bytes(fo, schema), 'utf-8')

def read_fixed(fo, schema):
    '''Fixed instances are encoded using the number of bytes declared in the
    schema.'''
    return fo.read(schema["size"])

def read_enum(fo, schema):
    '''An enum is encoded by a int, representing the zero-based position of the
    symbol in the schema.
    '''
    # read data
    return schema['type']['symbols'][read_int(fo, schema)]

def read_array(fo, schema):
    '''Arrays are encoded as a series of blocks.

    Each block consists of a long count value, followed by that many array
    items.  A block with count zero indicates the end of the array.  Each item
    is encoded per the array's item schema.

    If a block's count is negative, then the count is followed immediately by a
    long block size, indicating the number of bytes in the block.  The actual
    count in this case is the absolute value of the count written.
    '''
    read_items = []

    block_count = read_long(fo, schema)

    while block_count != 0:
        if block_count < 0:
            block_count = -block_count
            block_size = decoder.read_long(fo, schema)

        for i in xrange(block_count):
            read_items.append(read_data(fo, schema['items']))
            block_count = read_long(fo, schema)

    return read_items

def read_map(fo, schema):
    '''Maps are encoded as a series of blocks.

    Each block consists of a long count value, followed by that many key/value
    pairs.  A block with count zero indicates the end of the map.  Each item is
    encoded per the map's value schema.

    If a block's count is negative, then the count is followed immediately by a
    long block size, indicating the number of bytes in the block.  The actual
    count in this case is the absolute value of the count written.
    '''
    read_items = {}
    block_count = read_long(fo, schema)
    while block_count != 0:
        if block_count < 0:
            block_count = -block_count
            block_size = read_long(fo, schema)

        for i in range(block_count):
            key = read_utf8(fo, schema)
            read_items[key] = read_data(fo, schema['values'])
        block_count = read_long(fo, schema)

    return read_items

def read_union(fo, schema):
    '''A union is encoded by first writing a long value indicating the
    zero-based position within the union of the schema of its value.

    The value is then encoded per the indicated schema within the union.
    '''
    # schema resolution
    index = read_long(fo, schema)
    return read_data(fo, schema[index])

def read_record(fo, schema):
    '''A record is encoded by encoding the values of its fields in the order
    that they are declared. In other words, a record is encoded as just the
    concatenation of the encodings of its fields.  Field values are encoded per
    their schema.

    Schema Resolution:
     * the ordering of fields may be different: fields are matched by name.
     * schemas for fields with the same name in both records are resolved
         recursively.
     * if the writer's record contains a field with a name not present in the
         reader's record, the writer's value for that field is ignored.
     * if the reader's record schema has a field that contains a default value,
         and writer's schema does not have a field with the same name, then the
         reader should use the default value from its field.
     * if the reader's record schema has a field with no default value, and
         writer's schema does not have a field with the same name, then the
         field's value is unset.
    '''
    # schema resolution
    record = {}
    for field in schema['fields']:
        record[field['name']] = read_data(fo, field['type'])

    return record

#     # FIXME: defaults, does not work currently
#     if len(record) == len(rschema['fields']):
#         return record
# 
#     # fill in default values
#     for field in schema["fields"]:
#         name = field["name"]
#         if name in record:
#             continue
#         assert 'default' in field, 'no default for {name}'.format(field)
# 
#         record[name] = default_value(field)
# 
#     return record

# def identity(x):
#     return x
# 
# def default_value(field):
#     default = field["default"]
# 
#     return {
#         "null" : lambda _: None,
#         "boolean" : bool,
#         "int" : int,
#         "long" : long,
#         "float" : float,
#         "double" : float,
#         "enum" : identity,
#         "fixed" : identity,
#         "string" : identity,
#         "bytes" : identity,
#         "array" : lambda vals: [default_value(v) for v in vals],
#         "map" : lambda vals: dict((k, v) for k, v in vals),
#     }[field["type"]](default)

        # FIXME: 
#     elif field_schema.type in ['union', 'error_union']:
#       return self._read_default_value(field_schema.schemas[0], default_value)
#     elif field_schema.type == 'record':
#       read_record = {}
#       for field in field_schema.fields:
#         json_val = default_value.get(field.name)
#         if json_val is None: json_val = field.default
#         field_val = self._read_default_value(field.type, json_val)
#         read_record[field.name] = field_val
#       return read_record
#     else:
#       fail_msg = 'Unknown type: %s' % field_schema.type
#       raise schema.AvroException(fail_msg)
#     # FIXME
#     pass
# 

READERS = {
    'null' : read_null,
    'boolean' : read_boolean,
    'string' : read_utf8,
    'int' : read_long,
    'long' : read_long,
    'float' : read_float,
    'double' : read_double,
    'bytes' : read_bytes,
    'fixed' : read_fixed,
    'enum' : read_enum,
    'array' : read_array,
    'map' : read_map,
    'union' : read_union,
    'error_union' : read_union,
    'record' : read_record,
    'error' : read_record,
    'request' : read_record,
}

def read_data(fo, schema):
    st = type(schema)
    if st == dict:
        record_type = schema['type']
    elif st == list:
        record_type = 'union'
    else:
        record_type = schema

    reader = READERS[record_type]
    return reader(fo, schema)

def skip_sync(fo, sync_marker):
    mark = fo.read(SYNC_SIZE)
    if mark != sync_marker:
        fo.seek(-SYNC_SIZE, SEEK_CUR)

def _iter_avro(fo, header, schema):
    sync_marker = header['sync']
    codec = header['meta'].get('avro.codec', 'null')
    block_count = 0
    block_fo = fo
    while True:
        skip_sync(fo, sync_marker)
        block_count = read_long(fo, None)
        if codec == 'null':
            read_long(fo, None)
            block_fo = fo
        else:
            data = read_bytes(fo, None)
            block_fo = StringIO(decompress(data))

        for i in xrange(block_count):
            yield read_data(block_fo, schema)

class iter_avro:
    def __init__(self, fo):
        self.fo = fo
        self._header = read_data(fo, META_SCHEMA)
        self.schema = json.loads(self._header['meta']['avro.schema'])
        self._records = _iter_avro(fo, self._header, self.schema)

    def next(self):
        try:
            return next(self._records)
        except EOFError:
            raise StopIteration

    def __iter__(self):
        return self

def main(argv=None):
    import sys
    from argparse import ArgumentParser

    argv = argv or sys.argv

    parser = ArgumentParser(description='iter over avro file')
    parser.add_argument('filename', help='file to parse')
    args = parser.parse_args(argv[1:])

    for r in iter_avro(open(args.filename, 'rb')):
        print r


if __name__ == '__main__':
    main()
