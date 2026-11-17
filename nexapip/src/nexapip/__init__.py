"""NexaPip provides Python bindings for Nexa Launcher.

You don't need to include all the submodules, all needed classes are automatically included in the package scope upon
importing the package. """

__version__ = "0.2.0"

__all__ = ['APP_API_VERSION', 'Activity', 'App', 'Buffer', 'Button', 'Checkbox', 'CompoundButton', 'Connection', 'EditText', 'Event',
           'FrameLayout', "GridLayout", 'HorizontalScrollView', 'ImageView', 'LinearLayout', 'NestedScrollView',
           'NexaApp', 'Notification', 'ProgressBar', 'RadioButton', 'RadioGroup', 'RemoteViews', 'Space', 'Spinner',
           'SwipeRefreshLayout', 'Switch', 'SurfaceView', 'TabLayout', 'Task', 'TextView', 'ToggleButton', 'View', 'ViewGroup',
           'WebView']

from nexapip.activity import Activity
from nexapip.application import APP_API_VERSION, NexaApp
from nexapip.buffer import Buffer
from nexapip.button import Button
from nexapip.checkbox import Checkbox
from nexapip.compoundbutton import CompoundButton
from nexapip.connection import Connection
from nexapip.edittext import EditText
from nexapip.event import Event
from nexapip.framelayout import FrameLayout
from nexapip.gridlayout import GridLayout
from nexapip.horizontalscrollview import HorizontalScrollView
from nexapip.imageview import ImageView
from nexapip.linearlayout import LinearLayout
from nexapip.nestedscrollview import NestedScrollView
from nexapip.notification import Notification
from nexapip.progressbar import ProgressBar
from nexapip.radiobutton import RadioButton
from nexapip.radiogroup import RadioGroup
from nexapip.remoteviews import RemoteViews
from nexapip.space import Space
from nexapip.spinner import Spinner
from nexapip.swiperefreshlayout import SwipeRefreshLayout
from nexapip.switch import Switch
from nexapip.surfaceview import SurfaceView
from nexapip.tablayout import TabLayout
from nexapip.task import Task
from nexapip.textview import TextView
from nexapip.togglebutton import ToggleButton
from nexapip.view import View
from nexapip.viewgroup import ViewGroup
from nexapip.webview import WebView

App = NexaApp
