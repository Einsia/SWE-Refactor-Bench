"""Minimal, dependency-free Java class file reader.

Only what the verifier needs: version, this-class name, and the Module
attribute (JVMS 4.7.25). Written so the verifier never needs a JDK on PATH to
reason about `module-info.class`.
"""
import struct

# constant pool tag -> byte length of the payload that follows the tag byte,
# or None when the entry needs special handling.
_FIXED = {
    3: 4, 4: 4, 5: 8, 6: 8,      # Integer Float Long Double
    7: 2, 8: 2, 16: 2, 19: 2, 20: 2,  # Class String MethodType Module Package
    9: 4, 10: 4, 11: 4, 12: 4, 17: 4, 18: 4,  # *ref NameAndType Dynamic
    15: 3,                        # MethodHandle
}
_WIDE = (5, 6)  # Long and Double occupy two pool slots


class ClassFile(object):
    def __init__(self, data):
        if data[:4] != b'\xca\xfe\xba\xbe':
            raise ValueError('not a class file')
        self.minor, self.major = struct.unpack('>HH', data[4:8])
        self.pool = {}
        off = 10
        count = struct.unpack('>H', data[8:10])[0]
        i = 1
        while i < count:
            tag = data[off]
            off += 1
            if tag == 1:
                n = struct.unpack('>H', data[off:off + 2])[0]
                self.pool[i] = ('utf8', data[off + 2:off + 2 + n].decode('utf-8'))
                off += 2 + n
            elif tag in _FIXED:
                self.pool[i] = (tag, data[off:off + _FIXED[tag]])
                off += _FIXED[tag]
            else:
                raise ValueError('unknown constant pool tag %d' % tag)
            i += 2 if tag in _WIDE else 1
        self._data = data
        self._off = off

    # -- constant pool accessors -------------------------------------------
    def utf8(self, index):
        kind, val = self.pool[index]
        if kind != 'utf8':
            raise ValueError('cp#%d is not utf8' % index)
        return val

    def _name_of(self, index, tag):
        if index == 0:
            return None
        kind, payload = self.pool[index]
        if kind != tag:
            raise ValueError('cp#%d is tag %r not %r' % (index, kind, tag))
        return self.utf8(struct.unpack('>H', payload)[0])

    def class_name(self, index):
        return self._name_of(index, 7)

    def module_name(self, index):
        return self._name_of(index, 19)

    def package_name(self, index):
        return self._name_of(index, 20)

    def strings(self):
        """Every String constant in the pool.

        A `static final String` initialised with a literal is a compile-time
        constant, so its value is a CONSTANT_String here regardless of whether
        any code reads it.
        """
        out = []
        for idx, (kind, payload) in sorted(self.pool.items()):
            if kind == 8:
                try:
                    out.append(self.utf8(struct.unpack('>H', payload)[0]))
                except (KeyError, ValueError):
                    pass
        return out

    def utf8s(self):
        """Every UTF-8 entry: names, descriptors and literals alike."""
        return [v for _k, (kind, v) in sorted(self.pool.items())
                if kind == 'utf8']

    def field_names(self):
        """The names in the field table, in declaration order.

        Read by parsing rather than by reflection because a check on a jar entry
        must not need the class to load: GsonBuildConfig loads fine, but the
        release-9 module descriptor beside it does not load on any classpath.
        """
        self.parse()
        return list(self._fields)

    def method_names(self):
        self.parse()
        return list(self._methods)

    # -- structure ----------------------------------------------------------
    def parse(self):
        d, off = self._data, self._off
        access, this_cls, _super = struct.unpack('>HHH', d[off:off + 6])
        off += 6
        n_iface = struct.unpack('>H', d[off:off + 2])[0]
        off += 2 + 2 * n_iface
        members = ([], [])
        for table in members:  # fields then methods
            n = struct.unpack('>H', d[off:off + 2])[0]
            off += 2
            for _ in range(n):
                table.append(self.utf8(struct.unpack('>H', d[off + 2:off + 4])[0]))
                off += 6
                na = struct.unpack('>H', d[off:off + 2])[0]
                off += 2
                for _ in range(na):
                    length = struct.unpack('>I', d[off + 2:off + 6])[0]
                    off += 6 + length
        self._fields, self._methods = members
        attrs = {}
        n = struct.unpack('>H', d[off:off + 2])[0]
        off += 2
        for _ in range(n):
            name = self.utf8(struct.unpack('>H', d[off:off + 2])[0])
            length = struct.unpack('>I', d[off + 2:off + 6])[0]
            attrs[name] = d[off + 6:off + 6 + length]
            off += 6 + length
        self.access = access
        self.this_class = self.class_name(this_cls)
        self.attributes = attrs
        return self

    def module(self):
        """Decode the Module attribute into plain dicts, or None."""
        raw = self.attributes.get('Module')
        if raw is None:
            return None
        u2 = lambda o: struct.unpack('>H', raw[o:o + 2])[0]
        off = 0
        name = self.module_name(u2(0))
        flags = u2(2)
        version_idx = u2(4)
        off = 6
        out = {
            'name': name,
            'open': bool(flags & 0x0020),
            'version': self.utf8(version_idx) if version_idx else None,
            'requires': [], 'exports': [], 'opens': [],
            'uses': [], 'provides': [],
        }
        n = u2(off); off += 2
        for _ in range(n):
            req = self.module_name(u2(off))
            rflags = u2(off + 2)
            vidx = u2(off + 4)
            off += 6
            out['requires'].append({
                'name': req,
                'transitive': bool(rflags & 0x0020),
                'static': bool(rflags & 0x0040),
                'version': self.utf8(vidx) if vidx else None,
            })
        for key in ('exports', 'opens'):
            n = u2(off); off += 2
            for _ in range(n):
                pkg = (self.package_name(u2(off)) or '').replace('/', '.')
                eflags = u2(off + 2)
                cnt = u2(off + 4)
                off += 6
                to = []
                for _ in range(cnt):
                    to.append(self.module_name(u2(off)))
                    off += 2
                out[key].append({'package': pkg, 'flags': eflags, 'to': sorted(to)})
        n = u2(off); off += 2
        for _ in range(n):
            out['uses'].append(self.class_name(u2(off)))
            off += 2
        n = u2(off); off += 2
        for _ in range(n):
            svc = self.class_name(u2(off))
            cnt = u2(off + 2)
            off += 4
            impls = []
            for _ in range(cnt):
                impls.append(self.class_name(u2(off)))
                off += 2
            out['provides'].append({'service': svc, 'with': sorted(impls)})
        for key in ('requires', 'exports', 'opens', 'provides'):
            out[key].sort(key=lambda e: str(sorted(e.items())))
        out['uses'].sort()
        return out


def major_version(data):
    if data[:4] != b'\xca\xfe\xba\xbe':
        raise ValueError('not a class file')
    return struct.unpack('>H', data[6:8])[0]
