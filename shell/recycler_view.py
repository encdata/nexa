import base64
import json
from concurrent.futures import CancelledError, ThreadPoolExecutor
from threading import Lock

import termuxgui as tg


class RecyclerListView(tg.View):
    def __init__(self, activity, parent=None, click=True, longclick=True):
        params = {
            "aid": activity.aid,
            "click": click,
            "longclick": longclick,
        }
        if parent is not None:
            params["parent"] = parent.id
        view_id = activity.c.send_read_msg({"method": "createRecyclerView", "params": params})
        super().__init__(activity, view_id)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nexa-list")
        self._state_lock = Lock()
        self._generation = 0
        self._closed = False
        self._update = None

    def setitems(self, items, keep_scroll=False):
        with self._state_lock:
            if self._closed:
                return
            self._generation += 1
            generation = self._generation
            if self._update is not None:
                self._update.cancel()
            self._update = self._executor.submit(
                self._send_items, generation, items, keep_scroll)
            self._update.add_done_callback(self._report_error)

    @staticmethod
    def _report_error(future):
        try:
            future.result()
        except CancelledError:
            pass
        except Exception as error:
            print(f"nexa: couldn't update recycler list: {error}")

    def _send_items(self, generation, items, keep_scroll):
        rows = []
        image_keys = {}
        images = {}
        for index, item in enumerate(items):
            if index % 16 == 0:
                with self._state_lock:
                    if self._closed or generation != self._generation:
                        return
            image = item.get("image")
            image_key = None
            if image:
                image_key = image_keys.get(image)
                if image_key is None:
                    image_key = str(len(images))
                    image_keys[image] = image_key
                    images[image_key] = base64.b64encode(image).decode("ascii")
            rows.append({
                "text": item.get("text", ""),
                "subtitle": item.get("subtitle", ""),
                "marker": item.get("marker", ""),
                "imageKey": image_key,
                "key": item.get("key", ""),
                "cpu": item.get("cpu", ""),
                "memory": item.get("memory", ""),
                "background": item.get("background"),
            })
        message = json.dumps({"method": "setRecyclerItems", "params": {
            "aid": self.a.aid,
            "id": self.id,
            "items": rows,
            "images": images,
            "keepScroll": keep_scroll,
        }})
        with self._state_lock:
            if self._closed or generation != self._generation:
                return
        self.a.c.send_msg(message)

    def close(self):
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            self._generation += 1
            if self._update is not None:
                self._update.cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)
