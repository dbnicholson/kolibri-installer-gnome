import itertools

from gi.repository import Gio
from gi.repository import GLib
from gi.repository import GObject

from . import config

from .globals import KOLIBRI_USE_SYSTEM_INSTANCE


class KolibriDaemonProxy(Gio.DBusProxy):
    PROPERTY_MAP = {
        "AppKey": "app_key",
        "BaseURL": "base_url",
        "Status": "status",
        "KolibriHome": "kolibri_home",
        "Version": "version",
    }

    def __init__(self):
        super().__init__(
            g_bus_type=Gio.BusType.SYSTEM,
            g_name=config.DAEMON_APPLICATION_ID,
            g_object_path="/org/learningequality/Kolibri/Devel/Daemon",
            g_interface_name="org.learningequality.Kolibri.Daemon",
        )

    def do_g_properties_changed(self, changed_properties, invalidated_properties):
        dbus_properties = itertools.chain(
            changed_properties.keys(), invalidated_properties
        )
        local_properties = [
            self.PROPERTY_MAP[dbus_property]
            for dbus_property in dbus_properties
            if dbus_property in self.PROPERTY_MAP
        ]

        for property_name in local_properties:
            self.notify(property_name)

    @GObject.Property
    def app_key(self):
        variant = self.get_cached_property("AppKey")
        return variant.get_string() if variant else None

    @GObject.Property
    def base_url(self):
        variant = self.get_cached_property("BaseURL")
        return variant.get_string() if variant else None

    @GObject.Property
    def status(self):
        variant = self.get_cached_property("Status")
        return variant.get_string() if variant else None

    @GObject.Property
    def kolibri_home(self):
        variant = self.get_cached_property("KolibriHome")
        return variant.get_string() if variant else None

    @GObject.Property
    def version(self):
        variant = self.get_cached_property("Version")
        return variant.get_uint32() if variant else None

    def hold(self):
        return self.Hold()

    def release(self):
        return self.Release()

    def start(self):
        return self.Start()

    def get_item_ids_for_search(self, search):
        return self.GetItemIdsForSearch("(s)", search)

    def get_metadata_for_item_ids(self, item_ids):
        return self.GetMetadataForItemIds("(as)", item_ids)

    def is_loading(self):
        if not self.app_key or not self.base_url:
            return True
        else:
            return self.status in ["NONE", "STARTING"]

    def is_started(self):
        if self.app_key and self.base_url:
            return self.status in ["STARTED"]
        else:
            return False

    def is_error(self):
        return self.status in ["ERROR"]

    def is_kolibri_app_url(self, url):
        if callable(url):
            return True

        if not url or not self.base_url:
            return False
        elif not url.startswith(self.base_url):
            return False
        elif url.startswith(self.base_url + "static/"):
            return False
        elif url.startswith(self.base_url + "downloadcontent/"):
            return False
        elif url.startswith(self.base_url + "content/storage/"):
            return False
        else:
            return True

    def get_initialize_url(self, next_url):
        if callable(next_url):
            next_url = next_url()
        return self.__get_kolibri_initialize_url(next_url)

    def __get_kolibri_initialize_url(self, next_url):
        path = "app/api/initialize/{key}".format(key=self.app_key)
        if next_url:
            path += "?next={next_url}".format(next_url=next_url)
        return self.base_url + path.lstrip("/")