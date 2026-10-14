from advi.core.runtime import Runtime


class Resource:
    def __init__(self, name, events):
        self.name = name
        self.events = events

    def close(self):
        self.events.append(self.name)


def test_runtime_closes_registered_resources_in_reverse_order(tmp_path):
    runtime = Runtime(settings=object())
    events = []
    first = Resource("first", events)
    second = Resource("second", events)

    runtime.register_resource(first)
    runtime.register_resource(second)
    runtime.shutdown()

    assert events == ["second", "first"]


def test_runtime_cleanup_is_idempotent(tmp_path):
    runtime = Runtime(settings=object())
    events = []
    runtime.register_resource(Resource("only", events))

    runtime.shutdown()
    runtime.shutdown()

    assert events == ["only"]


def test_output_manager_close_delegates_to_tts(tmp_path):
    from advi.io.output import OutputManager

    class TTS:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    tts = TTS()
    output = OutputManager(tts=tts)
    output.close()

    assert tts.closed is True
