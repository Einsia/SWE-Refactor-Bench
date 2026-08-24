/* Surface.java -- dumps the submitted jar's public API as seen from inside a JVM.
 *
 * structure.py grades the API against the contract twice, from two independent
 * readings of the same jar, and this is the second one.
 *
 * The first reading parses the class files as bytes.  That is enough to answer
 * almost every structural question, and it cannot be fooled by a class that
 * refuses to initialize.  What it cannot answer is whether the classes actually
 * *load*: a class file can parse perfectly and still fail verification, fail to
 * resolve its superclass, or throw out of its static initializer -- and a jar
 * whose Zlib class throws on first touch is unusable no matter how well formed its
 * bytes are.  Reflection answers that by construction, because getting the answer
 * at all required loading the class.
 *
 * The two readings also disagree in one interesting place, deliberately.  A
 * `public static final int` has its value in a ConstantValue attribute *and*
 * potentially in the static initializer, and nothing makes them agree: a class can
 * declare ConstantValue 0 and assign 7 in <clinit>.  javac inlines the
 * ConstantValue at every use site, so a consumer compiled against the jar sees 0
 * while a consumer that reads the field reflectively sees 7.  That is an ABI split
 * with no C analogue, and catching it needs both numbers -- so this dumps the
 * reflected value and structure.py compares it against both the contract and the
 * parsed attribute.
 *
 * Design rule: this program knows how to *look*, not what is correct.  It takes
 * the class names to inspect on the command line and prints everything public it
 * finds, sorted; every expectation lives in the contract and every comparison
 * happens in structure.py.  The two exceptions are the `behavior` records at the
 * end, which have to call methods to answer, and each carries a comment saying why
 * it could not be a differential probe case instead.
 *
 * usage: Surface <class-name>...
 * output: sorted records on stdout, one per line, tab-free and LF-terminated.
 */

import java.lang.reflect.Constructor;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.lang.reflect.Modifier;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;

public final class Surface {

    private static final List<String> RECORDS = new ArrayList<>();

    private static void add(String record) {
        RECORDS.add(record);
    }

    /* Type names in the contract are written the way source writes them:
     * "byte[]", "java.lang.String", "int".  Class.getName() writes arrays as
     * "[B" and "[Ljava.lang.String;", so every type is normalized here rather
     * than in the comparison -- a normalizer on the Python side would have to
     * re-implement JVM descriptor rules to do it, and that is exactly the kind
     * of duplicated parsing that drifts. */
    private static String typeName(Class<?> type) {
        if (type.isArray()) {
            return typeName(type.getComponentType()) + "[]";
        }
        return type.getName();
    }

    private static String typeList(Class<?>[] types) {
        StringBuilder out = new StringBuilder();
        for (int i = 0; i < types.length; i++) {
            if (i > 0) {
                out.append(',');
            }
            out.append(typeName(types[i]));
        }
        return out.toString();
    }

    /* Only the modifiers that are part of the published contract.  ACC_SYNTHETIC
     * and friends are deliberately absent: they are compiler bookkeeping, and a
     * contract that pinned them would be pinning javac's implementation rather
     * than the interface. */
    private static String mods(int m) {
        List<String> names = new ArrayList<>();
        if (Modifier.isPublic(m)) {
            names.add("public");
        }
        if (Modifier.isProtected(m)) {
            names.add("protected");
        }
        if (Modifier.isPrivate(m)) {
            names.add("private");
        }
        if (Modifier.isStatic(m)) {
            names.add("static");
        }
        if (Modifier.isFinal(m)) {
            names.add("final");
        }
        if (Modifier.isAbstract(m)) {
            names.add("abstract");
        }
        if (Modifier.isNative(m)) {
            names.add("native");
        }
        if (Modifier.isSynchronized(m)) {
            names.add("synchronized");
        }
        if (Modifier.isVolatile(m)) {
            names.add("volatile");
        }
        if (Modifier.isTransient(m)) {
            names.add("transient");
        }
        return names.isEmpty() ? "-" : String.join("+", names);
    }

    private static void dumpClass(Class<?> k) {
        String kind = k.isInterface() ? "interface" : (k.isEnum() ? "enum" : "class");
        Class<?> parent = k.getSuperclass();
        List<String> ifaces = new ArrayList<>();
        for (Class<?> i : k.getInterfaces()) {
            ifaces.add(typeName(i));
        }
        Collections.sort(ifaces);
        add(String.format(
                "class %s kind=%s mods=%s super=%s ifaces=%s",
                typeName(k), kind, mods(k.getModifiers()),
                parent == null ? "-" : typeName(parent),
                ifaces.isEmpty() ? "-" : String.join(",", ifaces)));

        /* getDeclaredFields rather than getFields: inherited members are reported
         * against the class that declares them, so ZStream's fields are not
         * counted a second time under Deflater.  The "no extra public member"
         * case depends on that -- a surface that widened by inheritance widened at
         * the declaring class, and that is where it should be reported. */
        for (Field f : k.getDeclaredFields()) {
            if (f.isSynthetic()) {
                continue;
            }
            add(String.format(
                    "field %s#%s type=%s mods=%s",
                    typeName(k), f.getName(), typeName(f.getType()),
                    mods(f.getModifiers())));
            /* Values, for the constants.  Only public static final, and only when
             * the read succeeds: a private field's value is none of the
             * verifier's business, and asking would need setAccessible on a module
             * that is correctly not open. */
            int fm = f.getModifiers();
            if (Modifier.isPublic(fm) && Modifier.isStatic(fm) && Modifier.isFinal(fm)) {
                try {
                    Object value = f.get(null);
                    if (value instanceof Integer) {
                        add(String.format("const %s#%s = %d",
                                typeName(k), f.getName(), (Integer) value));
                    } else if (value instanceof String) {
                        add(String.format("sconst %s#%s = %s",
                                typeName(k), f.getName(), value));
                    } else if (value instanceof Long) {
                        add(String.format("lconst %s#%s = %d",
                                typeName(k), f.getName(), (Long) value));
                    }
                } catch (ReflectiveOperationException | RuntimeException e) {
                    add(String.format("constfail %s#%s = %s",
                            typeName(k), f.getName(), e.getClass().getName()));
                }
            }
        }

        for (Method m : k.getDeclaredMethods()) {
            if (m.isSynthetic() || m.isBridge()) {
                continue;
            }
            List<String> thrown = new ArrayList<>();
            for (Class<?> t : m.getExceptionTypes()) {
                thrown.add(typeName(t));
            }
            Collections.sort(thrown);
            add(String.format(
                    "method %s#%s(%s) returns=%s mods=%s throws=%s",
                    typeName(k), m.getName(), typeList(m.getParameterTypes()),
                    typeName(m.getReturnType()), mods(m.getModifiers()),
                    thrown.isEmpty() ? "-" : String.join(",", thrown)));
        }

        for (Constructor<?> c : k.getDeclaredConstructors()) {
            if (c.isSynthetic()) {
                continue;
            }
            List<String> thrown = new ArrayList<>();
            for (Class<?> t : c.getExceptionTypes()) {
                thrown.add(typeName(t));
            }
            Collections.sort(thrown);
            add(String.format(
                    "ctor %s(%s) mods=%s throws=%s",
                    typeName(k), typeList(c.getParameterTypes()),
                    mods(c.getModifiers()),
                    thrown.isEmpty() ? "-" : String.join(",", thrown)));
        }
    }

    /* -------------------------------------------------------------- behavior
     *
     * Two questions that cannot be asked as differential probe cases.  Both are
     * recorded as plain facts here; structure.py decides what they should be.
     */

    /* C's get_crc_table() returns a pointer to a static table, so calling it
     * twice yields the same pointer -- and a caller that wrote through it would
     * corrupt the library.  Java cannot hand out a reference to a shared int[]
     * and stay safe, so the contract requires a copy.  The two languages
     * therefore give *opposite* correct answers to "is it the same object twice",
     * which is precisely why this is not a probe case: the pair would have to
     * disagree by design, and a pair that is allowed to disagree anywhere is a
     * pair that cannot be compared byte for byte anywhere. */
    private static void dumpCrcTable() {
        try {
            Class<?> zlib = Class.forName("org.zlib.Zlib");
            Method crcTable = zlib.getMethod("crcTable");
            int[] first = (int[]) crcTable.invoke(null);
            int[] second = (int[]) crcTable.invoke(null);
            add("behavior crc-table-length = " + (first == null ? -1 : first.length));
            add("behavior crc-table-identity = " + (first == second ? "same" : "distinct"));
            if (first == null || first.length == 0) {
                add("behavior crc-table-mutation = unknown");
                return;
            }
            int original = first[0];
            first[0] = ~original;
            int[] third = (int[]) crcTable.invoke(null);
            add("behavior crc-table-mutation = "
                    + (third[0] == original ? "isolated" : "shared"));
            /* Restore, in case a later record in this same JVM reads it. */
            first[0] = original;
            add("behavior crc-table-entry0 = " + Integer.toUnsignedString(original));
        } catch (ReflectiveOperationException | RuntimeException e) {
            add("behavior crc-table-error = " + e.getClass().getName());
        }
    }

    /* C's inflateBackInit_ takes `unsigned char *window` and cannot see how long
     * it is, so it does not check -- a caller that passes a short window gets
     * memory corruption, and that is C's contract.  A Java implementation cannot
     * help seeing the length, and the contract requires it to return
     * Z_STREAM_ERROR rather than let an ArrayIndexOutOfBoundsException decide.
     * probe.c has no way to ask this, so it is asked here.
     *
     * Both directions are recorded.  A stub that returns Z_STREAM_ERROR for every
     * window would pass the short case and fail the exact-size one, so the exact
     * -size answer is what stops "always refuse" from being a valid answer. */
    private static void dumpBackWindow() {
        try {
            Class<?> back = Class.forName("org.zlib.InflateBack");
            Method init = back.getMethod("init", int.class, byte[].class);
            Method end = back.getMethod("end");
            Object shortWindow = back.getConstructor().newInstance();
            add("behavior back-window-short = "
                    + init.invoke(shortWindow, 15, new byte[1 << 10]));
            Object exact = back.getConstructor().newInstance();
            Object exactRc = init.invoke(exact, 15, new byte[1 << 15]);
            add("behavior back-window-exact = " + exactRc);
            if (Integer.valueOf(0).equals(exactRc)) {
                end.invoke(exact);
            }
            Object zero = back.getConstructor().newInstance();
            add("behavior back-window-empty = "
                    + init.invoke(zero, 15, new byte[0]));
            Object nullWindow = back.getConstructor().newInstance();
            add("behavior back-window-null = "
                    + init.invoke(nullWindow, 15, (Object) null));
        } catch (ReflectiveOperationException | RuntimeException e) {
            /* The cause matters more than the wrapper: an
             * InvocationTargetException wrapping ArrayIndexOutOfBoundsException is
             * the finding this record exists to make, and reporting only
             * "InvocationTargetException" would hide it. */
            Throwable cause = e.getCause() == null ? e : e.getCause();
            add("behavior back-window-error = " + cause.getClass().getName());
        }
    }

    /* Which module the classes actually came from, at runtime.  On the module path
     * this must be the named module; on the class path it is the unnamed one.  It
     * is recorded rather than asserted because both are legitimate -- what the
     * record proves is that the class was loaded from the artifact under test and
     * not from somewhere else that happened to be earlier on the path. */
    private static void dumpProvenance(Class<?> k) {
        Module module = k.getModule();
        add(String.format("origin %s module=%s named=%b",
                typeName(k), module.getName() == null ? "-" : module.getName(),
                module.isNamed()));
    }

    public static void main(String[] args) {
        if (args.length == 0) {
            System.err.println("usage: Surface <class-name>...");
            System.exit(2);
        }
        boolean failed = false;
        for (String name : args) {
            try {
                Class<?> k = Class.forName(name);
                dumpClass(k);
                dumpProvenance(k);
            } catch (Throwable t) {
                /* Throwable, not Exception: a class whose static initializer
                 * throws an Error arrives as ExceptionInInitializerError or
                 * NoClassDefFoundError, and "the class does not load" is the most
                 * important thing this program can report. */
                add(String.format("missing %s error=%s", name,
                        t.getClass().getName()));
                failed = true;
            }
        }
        dumpCrcTable();
        dumpBackWindow();

        String[] sorted = RECORDS.toArray(new String[0]);
        Arrays.sort(sorted);
        StringBuilder out = new StringBuilder();
        for (String record : sorted) {
            out.append(record).append('\n');
        }
        System.out.print(out);
        System.out.flush();
        /* A non-zero exit when a class was missing, so a caller that only checks
         * the status still learns something went wrong -- but the records are
         * printed first, because the records are what says *what* went wrong. */
        System.exit(failed ? 3 : 0);
    }
}
