// One throwaway compilation unit, so the closure warmer has something to compile.
//
// `dependency:go-offline` resolves declared dependencies and plugins; it does not
// prove the resolved artifacts are a coherent compile classpath.  Touching one
// type from each of the four stacks the target uses -- Spring MVC, Actuator,
// Jackson XML, Micrometer -- turns "the jars are present" into "javac accepts
// them", which is a different and stronger claim, and it costs one file.
//
// The class is compiled at image-build time and never shipped.  Nothing in the
// task depends on it existing at run time, and nothing here is a hint about the
// migration: it is four import statements over the stack the instruction names.
package local.swerefactor;

import com.fasterxml.jackson.dataformat.xml.XmlMapper;
import io.micrometer.core.instrument.MeterRegistry;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.actuate.health.HealthIndicator;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

@SpringBootApplication
@RestController
public final class Warm {

    @GetMapping("/warm")
    public ResponseEntity<String> warm() {
        return ResponseEntity.ok("warm");
    }

    static HealthIndicator indicator() {
        return () -> org.springframework.boot.actuate.health.Health.up().build();
    }

    static String xml() throws Exception {
        return new XmlMapper().writeValueAsString(new int[]{1, 2, 3});
    }

    static void meter(MeterRegistry registry) {
        registry.counter("warm").increment();
    }

    public static void main(String[] args) {
        // Never invoked by anything in the task; present so the class is a
        // complete program rather than a fragment javac happens to accept.
        if (args.length == Integer.MAX_VALUE) {
            SpringApplication.run(Warm.class, args);
        }
    }
}
