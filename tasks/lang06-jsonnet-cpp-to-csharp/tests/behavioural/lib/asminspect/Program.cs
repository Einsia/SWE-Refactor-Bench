// Reads the metadata tables of every managed assembly in a publish directory and
// prints them as JSON for tests/audit.py to grade.
//
// Why metadata and not text: the audit gates ask whether a submission reaches
// native code or spawns a process.  Grepping the IL for "DllImport" answers a
// different and weaker question, because the attribute name does not survive into
// IL and a P/Invoke can be declared with a computed module name.  What cannot be
// hidden is the row: every P/Invoke has an ImplMap row naming a ModuleRef, and
// every external call has a MemberRef row.  The CLR needs them to run the code, so
// they are there whatever the source looked like.
//
// Output shape (consumed by audit.inspect_assemblies):
//   { "publishDir": str,
//     "assemblies": [ { "name", "path", "isExecutable", "ilOnly",
//                       "pinvokes": [{method, module, entryPoint}],
//                       "moduleRefs": [str], "attributes": [str],
//                       "memberRefs": [str], "hasNativeResources": bool } ],
//     "nativeFiles": [str], "unreadable": [ {path, error} ] }
//
// Exit codes: 0 on success, 3 on a usage or IO error.  Never a nonzero exit for a
// finding -- findings are the caller's business, failures are this tool's.

using System.Reflection.Metadata;
using System.Reflection.Metadata.Ecma335;   // MetadataTokens, TableIndex
using System.Reflection.PortableExecutable;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace AsmInspect;

internal static class Program
{
    // A managed assembly the runtime itself provides.  These are present in a
    // self-contained publish, they legitimately contain hundreds of P/Invokes, and
    // they are not the submission's code.  Anything whose name starts with one of
    // these prefixes is inventoried but not inspected.
    private static readonly string[] FrameworkPrefixes =
    {
        "System.", "Microsoft.", "netstandard", "mscorlib", "WindowsBase",
        "PresentationCore", "PresentationFramework",
    };

    private static readonly string[] FrameworkExact = { "System" };

    private static int Main(string[] args)
    {
        if (args.Length != 1)
        {
            Console.Error.WriteLine("usage: AsmInspect <publish-dir>");
            return 3;
        }

        var dir = Path.GetFullPath(args[0]);
        if (!Directory.Exists(dir))
        {
            Console.Error.WriteLine($"not a directory: {dir}");
            return 3;
        }

        var assemblies = new JsonArray();
        var nativeFiles = new JsonArray();
        var frameworkFiles = new JsonArray();
        var unreadable = new JsonArray();

        foreach (var path in Directory
                     .EnumerateFiles(dir, "*", SearchOption.AllDirectories)
                     .OrderBy(p => p, StringComparer.Ordinal))
        {
            var rel = Path.GetRelativePath(dir, path);
            var ext = Path.GetExtension(path).ToLowerInvariant();

            // A native shared object next to the assemblies is reported whatever
            // its extension, because that is how a P/Invoke target arrives.
            if (IsNativeElf(path))
            {
                // .NET's own apphost is a native ELF and is expected: it is the
                // launcher `dotnet publish` emits for an Exe.  Recognised by having
                // a same-named managed .dll beside it.
                var sibling = Path.ChangeExtension(path, ".dll");
                if (ext.Length == 0 && File.Exists(sibling))
                {
                    frameworkFiles.Add(JsonValue.Create($"{rel}  (apphost)"));
                }
                else
                {
                    nativeFiles.Add(JsonValue.Create(rel));
                }
                continue;
            }

            if (ext != ".dll" && ext != ".exe")
            {
                continue;
            }

            try
            {
                var info = Inspect(path, rel);
                if (info is null)
                {
                    // A .dll that is not managed at all: a native library that
                    // happens to use the Windows extension.
                    nativeFiles.Add(JsonValue.Create(rel));
                }
                else if (IsFramework(info.Name))
                {
                    frameworkFiles.Add(JsonValue.Create(rel));
                }
                else
                {
                    assemblies.Add(info.ToJson());
                }
            }
            catch (Exception ex)
            {
                unreadable.Add(new JsonObject
                {
                    ["path"] = rel,
                    ["error"] = $"{ex.GetType().Name}: {ex.Message}",
                });
            }
        }

        var doc = new JsonObject
        {
            ["tool"] = "AsmInspect/1.0",
            ["publishDir"] = dir,
            ["assemblies"] = assemblies,
            ["nativeFiles"] = nativeFiles,
            ["frameworkFiles"] = frameworkFiles,
            ["unreadable"] = unreadable,
        };

        // No custom JsonSerializerOptions: a custom instance must carry a
        // TypeInfoResolver before it can be marked read-only, and the default
        // options already write compact JSON.
        Console.Out.Write(doc.ToJsonString());
        Console.Out.Write('\n');
        return 0;
    }

    private static bool IsFramework(string name) =>
        FrameworkExact.Contains(name, StringComparer.Ordinal) ||
        FrameworkPrefixes.Any(p => name.StartsWith(p, StringComparison.Ordinal));

    private static bool IsNativeElf(string path)
    {
        try
        {
            using var fs = File.OpenRead(path);
            Span<byte> magic = stackalloc byte[4];
            if (fs.Read(magic) != 4)
            {
                return false;
            }
            return magic[0] == 0x7f && magic[1] == (byte)'E' &&
                   magic[2] == (byte)'L' && magic[3] == (byte)'F';
        }
        catch (IOException)
        {
            return false;
        }
        catch (UnauthorizedAccessException)
        {
            return false;
        }
    }

    private sealed class AsmInfo
    {
        public string Name = "";
        public string Path = "";
        public bool IsExecutable;
        public bool IlOnly;
        public bool HasNativeResources;
        public List<JsonObject> PInvokes = new();
        public SortedSet<string> ModuleRefs = new(StringComparer.Ordinal);
        public SortedSet<string> Attributes = new(StringComparer.Ordinal);
        public SortedSet<string> MemberRefs = new(StringComparer.Ordinal);
        public SortedSet<string> NativeResourceNames = new(StringComparer.Ordinal);

        public JsonObject ToJson()
        {
            var pi = new JsonArray();
            foreach (var p in PInvokes)
            {
                pi.Add(p);
            }
            return new JsonObject
            {
                ["name"] = Name,
                ["path"] = Path,
                ["isExecutable"] = IsExecutable,
                ["ilOnly"] = IlOnly,
                ["hasNativeResources"] = HasNativeResources,
                ["nativeResourceNames"] = ToArray(NativeResourceNames),
                ["pinvokes"] = pi,
                ["moduleRefs"] = ToArray(ModuleRefs),
                ["attributes"] = ToArray(Attributes),
                ["memberRefs"] = ToArray(MemberRefs),
            };
        }

        private static JsonArray ToArray(IEnumerable<string> items)
        {
            var a = new JsonArray();
            foreach (var s in items)
            {
                // JsonValue.Create rather than Add<T>: the generic overload can
                // produce a customized value node that the default serializer
                // refuses to write.
                a.Add(JsonValue.Create(s));
            }
            return a;
        }
    }

    private static AsmInfo? Inspect(string path, string rel)
    {
        using var fs = File.OpenRead(path);
        using var pe = new PEReader(fs);

        if (!pe.HasMetadata)
        {
            return null;   // native PE, or a resource-only file
        }

        var md = pe.GetMetadataReader();

        // A netmodule has no assembly row; fall back to the file name so it is
        // still inspected rather than skipped.
        var info = new AsmInfo
        {
            Path = rel,
            Name = md.IsAssembly
                ? md.GetString(md.GetAssemblyDefinition().Name)
                : System.IO.Path.GetFileNameWithoutExtension(path),
        };

        var corHeader = pe.PEHeaders.CorHeader;
        info.IlOnly = corHeader is not null &&
                      (corHeader.Flags & CorFlags.ILOnly) != 0;

        // `dotnet publish` puts the managed entry point in Foo.dll and emits Foo as
        // a native apphost, so PEHeaders.IsDll is true for both CLIs.  The entry
        // point token is therefore the only thing that makes an assembly an
        // executable one.
        info.IsExecutable =
            corHeader is not null &&
            corHeader.EntryPointTokenOrRelativeVirtualAddress != 0;

        CollectPInvokes(md, info);
        CollectModuleRefs(md, info);
        CollectAttributes(md, info);
        CollectMemberRefs(md, info);
        CollectNativePayloads(pe, md, info);

        return info;
    }

    // ImplMap: one row per P/Invoke declaration.  This is the table the runtime
    // consults to bind a managed method to a native export, so a P/Invoke that is
    // not here is not a P/Invoke.
    private static void CollectPInvokes(MetadataReader md, AsmInfo info)
    {
        foreach (var handle in md.MethodDefinitions)
        {
            var method = md.GetMethodDefinition(handle);
            var import = method.GetImport();
            if (import.Module.IsNil && import.Name.IsNil)
            {
                continue;
            }

            var module = import.Module.IsNil
                ? "(none)"
                : md.GetString(md.GetModuleReference(import.Module).Name);
            var entry = import.Name.IsNil ? "(none)" : md.GetString(import.Name);

            info.PInvokes.Add(new JsonObject
            {
                ["method"] = $"{TypeNameOf(md, method.GetDeclaringType())}::" +
                             md.GetString(method.Name),
                ["module"] = module,
                ["entryPoint"] = entry,
            });
        }
    }

    // ModuleRef: the native modules referenced, whether or not an ImplMap row
    // survived.  Checked separately because a DllImport can be built at runtime
    // from a ModuleRef via a function pointer, and because a bare ModuleRef with no
    // ImplMap is itself evidence of something unusual.
    private static void CollectModuleRefs(MetadataReader md, AsmInfo info)
    {
        var count = md.GetTableRowCount(TableIndex.ModuleRef);
        for (var i = 1; i <= count; i++)
        {
            var h = MetadataTokens.ModuleReferenceHandle(i);
            info.ModuleRefs.Add(md.GetString(md.GetModuleReference(h).Name));
        }
    }

    private static void CollectAttributes(MetadataReader md, AsmInfo info)
    {
        foreach (var h in md.CustomAttributes)
        {
            var attr = md.GetCustomAttribute(h);
            var name = AttributeTypeName(md, attr);
            if (name is not null)
            {
                info.Attributes.Add(name);
            }
        }
    }

    private static string? AttributeTypeName(MetadataReader md, CustomAttribute attr)
    {
        switch (attr.Constructor.Kind)
        {
            case HandleKind.MethodDefinition:
            {
                var ctor = md.GetMethodDefinition(
                    (MethodDefinitionHandle)attr.Constructor);
                return TypeNameOf(md, ctor.GetDeclaringType());
            }
            case HandleKind.MemberReference:
            {
                var mr = md.GetMemberReference(
                    (MemberReferenceHandle)attr.Constructor);
                return ParentName(md, mr.Parent);
            }
            default:
                return null;
        }
    }

    // MemberRef: every member called across an assembly boundary, rendered as
    // Namespace.Type::Member.  This is where Process.Start, NativeLibrary.Load and
    // Marshal.GetDelegateForFunctionPointer become visible.
    private static void CollectMemberRefs(MetadataReader md, AsmInfo info)
    {
        foreach (var h in md.MemberReferences)
        {
            var mr = md.GetMemberReference(h);
            var parent = ParentName(md, mr.Parent);
            if (parent is null)
            {
                continue;
            }
            info.MemberRefs.Add($"{parent}::{md.GetString(mr.Name)}");
        }
    }

    // A managed resource whose bytes are a native image is a smuggled binary: the
    // submission would extract it at runtime and exec or dlopen it.  The gate that
    // rejects native files on disk does not see this one, so it is checked here.
    private static void CollectNativePayloads(PEReader pe, MetadataReader md,
                                              AsmInfo info)
    {
        // The managed resources live in a blob whose RVA is in the CorHeader.  Each
        // resource is a 4-byte little-endian length followed by that many bytes, at
        // the offset the ManifestResource row records.
        PEMemoryBlock block = default;
        var rva = pe.PEHeaders.CorHeader?.ResourcesDirectory
                    .RelativeVirtualAddress ?? 0;
        if (rva != 0)
        {
            try
            {
                block = pe.GetSectionData(rva);
            }
            catch (BadImageFormatException)
            {
                // Leave block empty; the name check below still applies.
            }
        }

        foreach (var h in md.ManifestResources)
        {
            var res = md.GetManifestResource(h);
            if (!res.Implementation.IsNil)
            {
                continue;   // lives in another file; that file is scanned on its own
            }

            var name = md.GetString(res.Name);
            var flagged = false;

            if (block.Length > 0 && res.Offset <= int.MaxValue)
            {
                try
                {
                    var reader = block.GetReader();
                    reader.Offset = (int)res.Offset;
                    var len = reader.ReadInt32();
                    if (len >= 4 && reader.RemainingBytes >= 4)
                    {
                        var magic = reader.ReadBytes(4);
                        if (magic[0] == 0x7f && magic[1] == (byte)'E' &&
                            magic[2] == (byte)'L' && magic[3] == (byte)'F')
                        {
                            flagged = true;
                        }
                        else if (magic[0] == (byte)'M' && magic[1] == (byte)'Z')
                        {
                            flagged = true;
                        }
                    }
                }
                catch (BadImageFormatException)
                {
                    // A resource we cannot read is not evidence of anything.
                }
                catch (ArgumentOutOfRangeException)
                {
                }
            }

            // Name check as a second signal only: it catches a payload stored in a
            // form the magic test misses (compressed, say), and costs nothing.
            if (name.EndsWith(".so", StringComparison.OrdinalIgnoreCase) ||
                name.EndsWith(".dylib", StringComparison.OrdinalIgnoreCase) ||
                name.Contains("libjsonnet", StringComparison.OrdinalIgnoreCase))
            {
                flagged = true;
            }

            if (flagged)
            {
                info.HasNativeResources = true;
                info.NativeResourceNames.Add(name);
            }
        }
    }

    private static string? ParentName(MetadataReader md, EntityHandle parent)
    {
        switch (parent.Kind)
        {
            case HandleKind.TypeReference:
                return TypeRefName(md, (TypeReferenceHandle)parent);
            case HandleKind.TypeDefinition:
                return TypeNameOf(md, (TypeDefinitionHandle)parent);
            case HandleKind.TypeSpecification:
                return "(typespec)";
            case HandleKind.ModuleReference:
                return md.GetString(
                    md.GetModuleReference((ModuleReferenceHandle)parent).Name);
            case HandleKind.MethodDefinition:
            {
                var m = md.GetMethodDefinition((MethodDefinitionHandle)parent);
                return TypeNameOf(md, m.GetDeclaringType());
            }
            default:
                return null;
        }
    }

    private static string TypeRefName(MetadataReader md, TypeReferenceHandle h)
    {
        var tr = md.GetTypeReference(h);
        var name = md.GetString(tr.Name);
        var ns = tr.Namespace.IsNil ? "" : md.GetString(tr.Namespace);

        // A nested type's ResolutionScope is the enclosing TypeRef.
        if (tr.ResolutionScope.Kind == HandleKind.TypeReference)
        {
            var outer = TypeRefName(md, (TypeReferenceHandle)tr.ResolutionScope);
            return $"{outer}+{name}";
        }
        return ns.Length == 0 ? name : $"{ns}.{name}";
    }

    private static string TypeNameOf(MetadataReader md, TypeDefinitionHandle h)
    {
        if (h.IsNil)
        {
            return "(global)";
        }
        var td = md.GetTypeDefinition(h);
        var name = md.GetString(td.Name);
        var ns = td.Namespace.IsNil ? "" : md.GetString(td.Namespace);
        var declaring = td.GetDeclaringType();
        if (!declaring.IsNil)
        {
            return $"{TypeNameOf(md, declaring)}+{name}";
        }
        return ns.Length == 0 ? name : $"{ns}.{name}";
    }
}
