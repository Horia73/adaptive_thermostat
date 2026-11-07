"""Window contacts, slope autodetect fallback and linked window zones (ZoneCore)."""

from __future__ import annotations

from engine.zone import (
    WINDOW_SOURCE_AUTODETECT,
    WINDOW_SOURCE_CONTACT,
    WINDOW_SOURCE_NONE,
    ZoneConfig,
    ZoneCore,
)


def _core(window_detection: bool = True, now: float = 0.0) -> ZoneCore:
    core = ZoneCore(ZoneConfig(window_detection=window_detection, mode_change_block_s=0.0), now=now)
    core.on_outdoor(now, 5.0)
    core.on_temperature(now, 20.0)
    core.set_target(22.0)
    core.set_hvac(now, True)
    return core


def _heating(core: ZoneCore, t: float) -> None:
    status = core.update(t)
    assert status.want_heat
    core.on_actuators(t + 10, 1.0, True)


def _sharp_drop(core: ZoneCore, t0: float, start: float = 20.0) -> float:
    """Four reports falling 0.2 °C every 3 min (a textbook open window)."""
    t = t0
    value = start
    for _ in range(4):
        t += 180
        value -= 0.2
        core.on_temperature(t, round(value, 1))
    return t


def test_contact_open_stops_heating_and_close_resumes():
    core = _core()
    _heating(core, 60)
    core.set_window_contact(120, True)
    status = core.update(121)
    assert not status.want_heat and status.forced_stop
    assert status.window_open and status.window_source == WINDOW_SOURCE_CONTACT
    assert status.reason == "window open (contact)"
    # Contacts raise no dismissable alert and never time out.
    assert status.window_alert is None
    core.on_actuators(130, 0.0, False)
    for t in range(600, 3 * 3600, 600):  # sensor heartbeats while open
        core.on_temperature(121 + t, 20.0)
    status = core.update(121 + 3 * 3600)
    assert status.window_open and not status.want_heat
    core.dismiss_window(121 + 3 * 3600)
    assert core.update(121 + 3 * 3600 + 1).window_open
    # Closed: control resumes right away.
    t = 121 + 3 * 3600 + 60
    core.set_window_contact(t, False)
    status = core.update(t + 1)
    assert not status.window_open
    assert status.want_heat


def test_contact_events_are_reported_for_the_logbook():
    core = _core()
    core.update(10)
    core.set_window_contact(20, True)
    event = core.update(21).window_event
    assert event is not None and event.opened and event.reason == "contact"
    assert core.update(22).window_event is None
    core.set_window_contact(30, False)
    event = core.update(31).window_event
    assert event is not None and not event.opened


def test_contact_works_without_window_detection():
    core = _core(window_detection=False)
    _heating(core, 60)
    core.set_window_contact(120, True)
    status = core.update(121)
    assert status.window_open and not status.want_heat
    assert status.window_source == WINDOW_SOURCE_CONTACT
    core.set_window_contact(200, False)
    assert core.update(201).want_heat


def test_contact_reporting_disables_autodetect():
    core = _core()
    core.set_window_contact(0, False)
    core.update(30)
    t = _sharp_drop(core, 30)
    status = core.update(t + 5)
    assert not status.window_open
    assert status.window_source == WINDOW_SOURCE_CONTACT


def test_unavailable_contacts_fall_back_to_autodetect():
    core = _core()
    core.set_window_contact(0, False)
    core.set_window_contact(10, None)  # all contacts unavailable
    assert core.update(20).window_source == WINDOW_SOURCE_AUTODETECT
    t = _sharp_drop(core, 20)
    status = core.update(t + 5)
    assert status.window_open and status.window_alert is not None
    assert status.reason == "window open (autodetect)"
    # A contact reporting again takes over and closes the detected window.
    core.set_window_contact(t + 30, False)
    status = core.update(t + 31)
    assert not status.window_open and status.window_alert is None


def test_unavailable_contacts_without_autodetect():
    core = _core(window_detection=False)
    core.set_window_contact(0, None)
    core.update(20)
    t = _sharp_drop(core, 20)
    status = core.update(t + 5)
    assert not status.window_open
    assert status.window_source == WINDOW_SOURCE_NONE


def test_linked_window_blocks_and_releases():
    core = _core()
    _heating(core, 60)
    core.set_linked_windows(120, ["climate.living", "climate.bucatarie"])
    status = core.update(121)
    assert not status.want_heat and status.forced_stop
    assert not status.window_open  # own state stays own
    assert status.linked_window_open
    assert status.linked_window_open_zones == ("climate.bucatarie", "climate.living")
    assert status.reason == "linked window open: climate.bucatarie, climate.living"
    core.set_linked_windows(200, ["climate.living"])
    assert not core.update(201).want_heat
    core.set_linked_windows(300, [])
    status = core.update(301)
    assert not status.linked_window_open and status.want_heat


def test_own_autodetect_works_while_linked():
    core = _core()
    core.update(10)
    core.set_linked_windows(20, ["climate.living"])
    t = _sharp_drop(core, 20)
    status = core.update(t + 5)
    assert status.window_open and status.linked_window_open
    assert status.reason == "window open (autodetect)"


def test_no_learning_while_window_blocks():
    """Contact or linked blocks gate learning like the detector does, plus 30 min after."""
    for block, release in (
        (lambda c, t: c.set_window_contact(t, True), lambda c, t: c.set_window_contact(t, False)),
        (lambda c, t: c.set_linked_windows(t, ["climate.living"]), lambda c, t: c.set_linked_windows(t, [])),
    ):
        core = _core()
        core.update(600)
        assert core.observer.learn_params and core.observer.learn_d
        params = core.estimator.params
        block(core, 700)
        core.update(701)
        assert not core.observer.learn_params and not core.observer.learn_d
        # A cold draught: the temperature falls for an hour; nothing is learned.
        t = 701.0
        value = 20.0
        while t < 4300:
            t += 120
            value -= 0.05
            core.on_temperature(t, round(value, 2))
            assert not core.observer.learn_params
            core.update(t + 1)
        assert core.estimator.params == params
        release(core, 4400)
        core.update(4401)
        assert core.observer.learn_d
        assert not core.observer.learn_params  # still inside the post-window block
        core.update(4400 + core.config.window_data_block_s + 1)
        assert core.observer.learn_params


def test_window_block_drops_pending_validation():
    core = _core()
    _heating(core, 60)
    t = 70
    while core.burst is not None and t < 4000:
        t += 30
        core.update(t)
    assert core._pending_validation
    core.set_window_contact(t + 10, True)
    assert not core._pending_validation
