from __future__ import annotations

import logging
import platform
import re
import typing
from gettext import gettext as _
from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import SplitResult
from urllib.parse import urlencode
from urllib.parse import urlsplit

from gi.repository import Gio
from gi.repository import GLib
from gi.repository import GObject
from gi.repository import WebKit2
from kolibri_app.config import DATA_DIR
from kolibri_app.config import FRONTEND_APPLICATION_ID

from .kolibri_daemon_manager import KolibriDaemonManager
from .utils import await_properties
from .utils import get_localized_file
from .utils import map_properties

logger = logging.getLogger(__name__)


class KolibriContext(GObject.GObject):
    """
    Keeps track of global context related to accessing Kolibri over HTTP. A
    single KolibriContext object is shared between all Application,
    KolibriWindow, and KolibriWebView objects. Generates a WebKit2.WebContext
    with the appropriate cookies to enable Kolibri's app mode and to log in as
    the correct user. Use the session-status property or kolibri-ready signal to
    determine whether Kolibri is ready to use.
    """

    __webkit_web_context: WebKit2.WebContext
    __kolibri_daemon: KolibriDaemonManager
    __setup_helper: _KolibriSetupHelper
    __loader_url: str

    SESSION_STATUS_LOADING = 0
    SESSION_STATUS_READY = 1
    SESSION_STATUS_ERROR = 2

    session_status = GObject.Property(type=int, default=SESSION_STATUS_LOADING)

    __gsignals__ = {
        "kolibri-ready": (GObject.SIGNAL_RUN_FIRST, None, ()),
    }

    def __init__(self):
        GObject.GObject.__init__(self)

        cache_dir = Path(GLib.get_user_cache_dir(), FRONTEND_APPLICATION_ID)
        data_dir = Path(GLib.get_user_data_dir(), FRONTEND_APPLICATION_ID)
        cookies_filename = Path(data_dir, "cookies.sqlite")

        website_data_manager = WebKit2.WebsiteDataManager(
            base_cache_directory=cache_dir.as_posix(),
            base_data_directory=data_dir.as_posix(),
        )

        website_data_manager.get_cookie_manager().set_persistent_storage(
            cookies_filename.as_posix(), WebKit2.CookiePersistentStorage.SQLITE
        )

        loader_path = get_localized_file(
            Path(DATA_DIR, "assets", "_load-{}.html").as_posix(),
            Path(DATA_DIR, "assets", "_load.html"),
        )
        self.__loader_url = loader_path.as_uri()

        self.__webkit_web_context = WebKit2.WebContext(
            website_data_manager=website_data_manager
        )
        self.__kolibri_daemon = KolibriDaemonManager()
        self.__setup_helper = _KolibriSetupHelper(
            self.__webkit_web_context, self.__kolibri_daemon
        )

        map_properties(
            [
                (self.__kolibri_daemon, "has-error"),
                (self.__setup_helper, "is-setup-complete"),
            ],
            self.__update_session_status,
        )

    @property
    def default_url(self) -> str:
        return "x-kolibri-app:/"

    @property
    def webkit_web_context(self) -> WebKit2.WebContext:
        return self.__webkit_web_context

    def init(self):
        self.__kolibri_daemon.init()

    def shutdown(self):
        self.__kolibri_daemon.shutdown()

    def get_absolute_url(self, url: str) -> typing.Optional[str]:
        url_tuple = urlsplit(url)
        if url_tuple.scheme == "kolibri":
            target_url = self.parse_kolibri_url_tuple(url_tuple)
            return self.__kolibri_daemon.get_absolute_url(target_url)
        elif url_tuple.scheme == "x-kolibri-app":
            target_url = self.parse_x_kolibri_app_url_tuple(url_tuple)
            return self.__kolibri_daemon.get_absolute_url(target_url)
        return url

    def kolibri_api_get(self, *args, **kwargs) -> typing.Any:
        return self.__kolibri_daemon.kolibri_api_get(*args, **kwargs)

    def kolibri_api_get_async(self, *args, **kwargs):
        self.__kolibri_daemon.kolibri_api_get_async(*args, **kwargs)

    def should_load_url(self, url: str) -> bool:
        return (
            url == self.default_url
            or urlsplit(url).scheme in ("kolibri", "x-kolibri-app", "about")
            or self.is_url_in_scope(url)
        )

    def default_is_url_in_scope(self, url: str) -> bool:
        if not self.__kolibri_daemon.is_url_in_scope(url):
            return False

        url_tuple = urlsplit(url)
        url_path = url_tuple.path.lstrip("/")

        return not (
            url_path.startswith("static/")
            or url_path.startswith("downloadcontent/")
            or url_path.startswith("content/storage/")
            or url_path.startswith("facility/api/downloadcsvfile/")
            or url_path.startswith("api/logger/downloadcsvfile/")
        )

    def is_url_in_scope(self, url: str) -> bool:
        return self.default_is_url_in_scope(url)

    def get_loader_url(self, state: str) -> str:
        return self.__loader_url + "#" + state

    def parse_kolibri_url_tuple(self, url_tuple: SplitResult) -> str:
        """
        Parse a URL tuple according to the public Kolibri URL format. This format uses
        a single-character identifier for a node type - "t" for topic or "c"
        for content, followed by its unique identifier. It is constrained to
        opening content nodes or search pages.

        Examples:

        - kolibri:t/TOPIC_NODE_ID?search=addition
        - kolibri:c/CONTENT_NODE_ID
        - kolibri:?search=addition
        """

        url_path = url_tuple.path.lstrip("/")
        url_query = parse_qs(url_tuple.query, keep_blank_values=True)
        url_search = " ".join(url_query.get("search", []))

        node_type, _, node_id = url_path.partition("/")

        if node_type == "c":
            return self._get_kolibri_content_path(node_id, url_search)
        elif node_type == "t":
            # As a special case, don't include the search property for topic
            # nodes. This means Kolibri will always show a simple browsing
            # interface for a topic, instead of a search interface.
            return self._get_kolibri_topic_path(node_id, None)
        else:
            return self._get_kolibri_library_path(url_search)

    def _get_kolibri_content_path(self, node_id: str, search: str = None) -> str:
        if search:
            query = {"keywords": search, "last": "TOPICS_TOPIC_SEARCH"}
            return f"/learn#/topics/c/{node_id}?{urlencode(query)}"
        else:
            return f"/learn#/topics/c/{node_id}"

    def _get_kolibri_topic_path(self, node_id: str, search: str = None) -> str:
        if search:
            query = {"keywords": search}
            return f"/learn#/topics/t/{node_id}/search?{urlencode(query)}"
        else:
            return f"/learn#/topics/t/{node_id}"

    def _get_kolibri_library_path(self, search: str = None) -> str:
        if search:
            query = {"keywords": search}
            return f"/learn#/library?{urlencode(query)}"
        else:
            return "/learn/#/home"

    def url_to_x_kolibri_app(self, url: str) -> str:
        return urlsplit(url)._replace(scheme="x-kolibri-app", netloc="").geturl()

    def parse_x_kolibri_app_url_tuple(self, url_tuple: SplitResult) -> str:
        """
        Parse a URL tuple according to the internal Kolibri app URL format. This
        format is the same as Kolibri's URLs, but without the hostname or port
        number.

        - x-kolibri-app:/device
        """
        return url_tuple._replace(scheme="", netloc="").geturl()

    def __update_session_status(self, has_error: bool, is_setup_complete: bool):
        if has_error:
            self.props.session_status = KolibriContext.SESSION_STATUS_ERROR
        elif is_setup_complete:
            self.props.session_status = KolibriContext.SESSION_STATUS_READY
            self.emit("kolibri-ready")
        else:
            self.props.session_status = KolibriContext.SESSION_STATUS_LOADING


class _KolibriSetupHelper(GObject.GObject):
    """
    Helper to set up a Kolibri web session. This helper communicates with the
    Kolibri web service and with kolibri-daemon to create an "app mode" cookie,
    and logs in as the desktop user through the login token mechanism. If
    Kolibri has not been set up, it will automatically create a facility.
    """

    __webkit_web_context: WebKit2.WebContext
    __kolibri_daemon: KolibriDaemonManager

    __login_webview: WebKit2.WebView

    AUTOLOGIN_URL_TEMPLATE = "kolibri_desktop_auth_plugin/login/{token}"

    login_token = GObject.Property(type=str, default=None)

    is_app_key_cookie_ready = GObject.Property(type=bool, default=False)
    is_session_cookie_ready = GObject.Property(type=bool, default=False)
    is_facility_ready = GObject.Property(type=bool, default=False)

    is_setup_complete = GObject.Property(type=bool, default=False)

    def __init__(
        self,
        webkit_web_context: WebKit2.WebContext,
        kolibri_daemon: KolibriDaemonManager,
    ):
        GObject.GObject.__init__(self)

        self.__webkit_web_context = webkit_web_context
        self.__kolibri_daemon = kolibri_daemon

        self.__login_webview = WebKit2.WebView(web_context=self.__webkit_web_context)
        self.__login_webview.connect(
            "load-changed", self.__login_webview_on_load_changed
        )

        self.__kolibri_daemon.connect(
            "dbus-owner-changed", self.__kolibri_daemon_on_dbus_owner_changed
        )
        self.__kolibri_daemon.connect(
            "notify::app-key-cookie", self.__kolibri_daemon_on_notify_app_key_cookie
        )
        self.__kolibri_daemon.connect(
            "notify::is-started", self.__kolibri_daemon_on_notify_is_started
        )

        await_properties(
            [
                (self, "is-facility-ready"),
                (self, "login-token"),
            ],
            self.__on_await_facility_ready_and_login_token,
        )

        map_properties(
            [
                (self, "is-app-key-cookie-ready"),
                (self, "is-session-cookie-ready"),
                (self, "is-facility-ready"),
            ],
            self.__update_is_setup_complete,
        )

        self.__kolibri_daemon_on_notify_app_key_cookie(self.__kolibri_daemon)

    def __login_webview_on_load_changed(
        self, webview: WebKit2.WebView, load_event: WebKit2.LoadEvent
    ):
        # Show the main webview once it finishes loading.
        if load_event == WebKit2.LoadEvent.FINISHED:
            self.props.is_session_cookie_ready = True
            self.props.login_token = None

    def __kolibri_daemon_on_dbus_owner_changed(
        self, kolibri_daemon: KolibriDaemonManager
    ):
        self.props.is_session_cookie_ready = False

        if kolibri_daemon.do_automatic_login:
            kolibri_daemon.get_login_token(self.__kolibri_daemon_on_login_token_ready)
        else:
            self.props.is_session_cookie_ready = True

    def __kolibri_daemon_on_notify_is_started(
        self, kolibri_daemon: KolibriDaemonManager, pspec: GObject.ParamSpec = None
    ):
        self.props.is_facility_ready = False

        if not self.__kolibri_daemon.props.is_started:
            return

        if not self.__kolibri_daemon.do_automatic_login:
            # No automatic login so we don't need a facility:
            self.props.is_facility_ready = True
            return

        self.__kolibri_daemon.kolibri_api_get_async(
            "/api/public/v1/facility/",
            result_cb=self.__on_kolibri_api_facility_response,
        )

    def __on_kolibri_api_facility_response(self, data: typing.Any):
        if isinstance(data, list) and data:
            self.props.is_facility_ready = True
            return

        # There is no facility, so automatically provision the device:
        self.__automatic_device_provision()

    def __automatic_device_provision(self):
        # TODO: In the future, this could be done in kolibri-daemon itself by
        #       using a simple automatic_provision.json file in the Kolibri home
        #       template. We need to do it here for now because we are only
        #       using this configuration with automatic login, which is only
        #       enabled in certain cases.
        logger.info("Provisioning device…")
        facility_name = _("Kolibri on {host}").format(
            host=platform.node() or "localhost"
        )
        request_body_data = {
            "facility": {
                "name": facility_name,
                "learner_can_login_with_no_password": False,
            },
            "preset": "formal",
            "superuser": None,
            "language_id": None,
            "device_name": None,
            "settings": {
                "landing_page": "learn",
                "allow_other_browsers_to_connect": False,
            },
            "allow_guest_access": False,
        }
        self.__kolibri_daemon.kolibri_api_post_async(
            "/api/device/deviceprovision/",
            result_cb=self.__on_kolibri_api_deviceprovision_response,
            request_body=request_body_data,
        )

    def __on_kolibri_api_deviceprovision_response(self, data: dict):
        logger.info("Device provisioned.")
        self.props.is_facility_ready = True

    def __on_await_facility_ready_and_login_token(
        self, is_facility_ready: bool, login_token: str
    ):
        if self.props.is_session_cookie_ready:
            return

        login_url = self.__kolibri_daemon.get_absolute_url(
            self.AUTOLOGIN_URL_TEMPLATE.format(token=login_token)
        )

        self.__login_webview.load_uri(login_url)

    def __kolibri_daemon_on_login_token_ready(
        self, kolibri_daemon: KolibriDaemonManager, login_token: typing.Optional[str]
    ):
        self.props.login_token = login_token

        if login_token is None:
            # If we are unable to get a login token, pretend the session cookie
            # is ready so the app will proceed as usual. This should only happen
            # in an edge case where kolibri-daemon is running on the system bus
            # but is unable to communicate with AccountsService.
            self.props.is_session_cookie_ready = True

    def __kolibri_daemon_on_notify_app_key_cookie(
        self, kolibri_daemon: KolibriDaemonManager, pspec: GObject.ParamSpec = None
    ):
        self.props.is_app_key_cookie_ready = False

        if not self.__kolibri_daemon.props.app_key_cookie:
            return

        self.__webkit_web_context.get_cookie_manager().add_cookie(
            self.__kolibri_daemon.props.app_key_cookie,
            None,
            self.__on_app_key_cookie_ready,
        )

    def __on_app_key_cookie_ready(
        self, cookie_manager: WebKit2.CookieManager, result: Gio.Task
    ):
        self.props.is_app_key_cookie_ready = True

    def __update_is_setup_complete(self, *setup_flags):
        self.props.is_setup_complete = all(setup_flags)


class KolibriChannelContext(KolibriContext):
    """
    A KolibriContext subclass that overrides is_url_in_scope in such a way that
    the application will only show content belonging to a particular Kolibri
    channel.
    """

    __channel_id: str

    def __init__(self, channel_id: str):
        super().__init__()

        self.__channel_id = channel_id

    @property
    def default_url(self) -> str:
        return f"x-kolibri-app:{self.__default_path}"

    @property
    def __default_path(self) -> str:
        return f"/learn#/topics/t/{self.__channel_id}"

    def _get_kolibri_library_path(self, search: str = None) -> str:
        if search:
            query = {"keywords": search}
            return f"{self.__default_path}/search?{urlencode(query)}"
        else:
            return self.__default_path

    def is_url_in_scope(self, url: str) -> bool:
        # Allow the user to navigate to login and account management pages, as
        # well as URLs related to file storage and general-purpose APIs, but not
        # to other channels or the channel listing page.

        # TODO: This is costly and complicated. Instead, we should be able to
        #       ask the Kolibri web frontend to avoid showing links outside of
        #       the channel, and target external links to a new window.

        if not super().is_url_in_scope(url):
            return False

        url_tuple = urlsplit(url)
        url_path = url_tuple.path.lstrip("/")

        if re.match(
            r"^(app|static|downloadcontent|content\/storage|content\/static|content\/zipcontent)\/?",
            url_path,
        ):
            return True
        elif re.match(
            r"^(?P<lang>[\w\-]+\/)?(user|logout|redirectuser|learn\/app)\/?",
            url_path,
        ):
            return True
        elif re.match(
            r"^(?P<lang>[\w\-]+\/)?kolibri_desktop_auth_plugin\/?",
            url_path,
        ):
            return True
        elif re.match(r"^(?P<lang>[\w\-]+\/)?learn\/?", url_path):
            return self.__is_learn_fragment_in_channel(url_tuple.fragment)
        else:
            return False

    def __is_learn_fragment_in_channel(self, fragment: str) -> bool:
        fragment = fragment.lstrip("/")

        if re.match(r"^(content-unavailable|search)", fragment):
            return True

        contentnode_id = self.__contentnode_id_for_learn_fragment(fragment)

        if contentnode_id is None:
            return False

        if contentnode_id == self.__channel_id:
            return True

        response = self.kolibri_api_get(f"/api/content/contentnode/{contentnode_id}")

        if not isinstance(response, dict):
            return False

        contentnode_channel = response.get("channel_id")

        return contentnode_channel == self.__channel_id

    def __contentnode_id_for_learn_fragment(
        self, fragment: str
    ) -> typing.Optional[str]:
        pattern = r"^topics\/([ct]\/)?(?P<node_id>\w+)"
        match = re.match(pattern, fragment)
        if match:
            return match.group("node_id")

        return None
