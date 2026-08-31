import uuid
from datetime import datetime, timezone

from app.services.market_data.tick_engine import TickEngine


def test_queue_does_not_drop_a_late_subscribers_tick_under_heavy_global_load():
    # Regression test for a real bug: every tick cycle broadcasts one
    # message per globally active instrument to every registered queue,
    # regardless of what that specific connection subscribed to. With a
    # small queue and hundreds of globally active instruments (e.g. many
    # leaked/stale connections from other clients), a queue could fill up
    # with messages for instruments this connection doesn't even care
    # about before the one instrument it just subscribed to (last in dict
    # iteration order) ever got enqueued -- silently starving it forever.
    engine = TickEngine()
    queue = engine.register()

    # Simulate 300 other, pre-existing globally active instruments (as if
    # from other connections/leaked subscriptions).
    for _ in range(300):
        engine.subscribe(uuid.uuid4(), seed_price=100.0)

    # Now this connection's own instrument of interest subscribes last --
    # dict insertion order puts it at the very end of the active list.
    my_instrument_id = uuid.uuid4()
    engine.subscribe(my_instrument_id, seed_price=100.0)
    engine.set_real_price(my_instrument_id, 1234.5, "yahoo")

    now = datetime.now(timezone.utc).isoformat()
    active = [iid for iid, count in engine._subscriber_counts.items() if count > 0]
    assert active[-1] == my_instrument_id  # confirms it really is last

    for instrument_id in active:
        message = engine._next_tick_message(instrument_id, now)
        if not queue.full():
            queue.put_nowait(message)

    # Drain the queue and confirm our instrument's real-priced tick made it through.
    drained = []
    while not queue.empty():
        drained.append(queue.get_nowait())

    my_ticks = [m for m in drained if m["instrument_id"] == str(my_instrument_id)]
    assert len(my_ticks) == 1
    assert my_ticks[0]["source"] == "yahoo"
    assert my_ticks[0]["price"] == 1234.5


def test_get_current_price_still_correct_with_many_active_instruments():
    engine = TickEngine()
    for _ in range(300):
        engine.subscribe(uuid.uuid4(), seed_price=100.0)
    my_id = uuid.uuid4()
    engine.subscribe(my_id, seed_price=50.0)
    engine.set_real_price(my_id, 999.0, "delta")
    assert engine.get_current_price(my_id) == 999.0
