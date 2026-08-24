"""The second port, as a monitoring system would see it.

Dropwizard runs two Jetty connectors: the application on one, and an admin servlet
tree on another with its own health checks, metrics, thread dump and task
endpoints.  The target framework has an equivalent -- a management port with its
own endpoint set -- and the two are wired completely differently.  That makes this
the axis where the frameworks' idioms diverge most, and the axis a migration is
most likely to leave for later and never come back to.

Graded on what is observable and stable, which is not the same as everything
observable.  /ping's `pong` and /tasks' listing are compared in full.
/healthcheck is compared on the set of checks it reports and each one's healthy
flag, not its timings.  /metrics and /threads are compared structurally only:
their bodies are live JVM state and two runs of the SAME jar disagree about them.

Structurally still means something.  A submission that serves no /metrics, or
serves it as an HTML dashboard, fails those cases -- the mask covers the values,
not the existence of the endpoint or the shape of what it returns.
"""
from __future__ import annotations


def test_admin_root(pair):
    """The admin port's index: the operator's entry point, listing what is there."""
    pair("admin-root").check()


def test_ping(pair):
    """The simplest liveness check in the tree, and the one a load balancer is
    most likely to be pointed at.  Its body is two words and a newline, and the
    newline is part of it."""
    pair("admin-ping").check()


def test_healthcheck(pair):
    """The registered health checks and their verdicts.  This repository registers
    a graphhopper check of its own alongside the framework's deadlock check, so
    the answer proves both that the mechanism was ported and that the
    application's own check was re-registered into it."""
    pair("admin-healthcheck").check()


def test_task_listing(pair):
    """/tasks enumerates the administrative tasks the framework exposes.  A
    submission that ported the port but not the tasks answers this differently
    while /ping still works."""
    pair("admin-tasks").check()


def test_metrics_is_served_and_shaped_right(pair):
    """/metrics exists, answers JSON, and has the top-level structure a metrics
    consumer parses.  Values masked -- they are counters from a JVM that has been
    running for a different number of milliseconds on each side."""
    pair("admin-metrics").check()


def test_thread_dump_is_served(pair):
    """/threads answers plain text.  Its content is whatever the JVM's threads
    happen to be doing, which is why only its existence and media type are
    graded -- but an admin port without a thread dump has lost a diagnostic an
    operator relies on at exactly the wrong moment."""
    pair("admin-threads").check()


def test_unknown_admin_path(pair):
    """A 404 on the admin port.  Its error rendering is the admin tree's, not the
    application's, and conflating the two is a natural consequence of serving both
    from one dispatcher."""
    pair("admin-unknown").check()


def test_application_path_is_not_on_the_admin_port(pair):
    """/route on the admin port must NOT route.  The separation is the point of
    having two ports: a submission that serves everything everywhere has removed
    a boundary an operator uses to decide what to expose to the network."""
    pair("admin-app-path-on-admin").check()


def test_admin_path_is_not_on_the_application_port(pair):
    """And the converse: /ping on the application port must not answer as the
    admin one.  The pair is what establishes that two distinct trees exist rather
    than one tree on two sockets."""
    pair("admin-admin-path-on-app").check()


def test_every_admin_case_is_graded(cases_in):
    graded = sum(1 for name, obj in globals().items()
                 if name.startswith("test_") and callable(obj)) - 1
    expected = len(cases_in("admin"))
    assert graded == expected, (
        f"the corpus has {expected} admin case(s) and this module grades "
        f"{graded}."
    )
