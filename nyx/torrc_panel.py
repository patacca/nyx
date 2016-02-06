"""
Panel displaying the torrc or nyxrc with the validation done against it.
"""

import math
import curses

from nyx.util import expand_path, panel, tor_controller, ui_tools

from stem.control import State
from stem.util import log, str_tools

MAX_WRAP_PER_LINE = 8


class TorrcPanel(panel.Panel):
  """
  Renders the current torrc or nyxrc with syntax highlighting in a scrollable
  area.
  """

  def __init__(self, stdscr):
    panel.Panel.__init__(self, stdscr, 'torrc', 0)

    self.scroll = 0
    self.show_line_num = True     # shows left aligned line numbers
    self.strip_comments = False   # drops comments and extra whitespace

    # height of the content when last rendered (the cached value is invalid if
    # _last_content_height_args is None or differs from the current dimensions)

    self._last_content_height = 1
    self._last_content_height_args = None

    self.torrc_location = ''
    self.torrc_content = None

    # listens for tor reload (sighup) events

    controller = tor_controller()
    controller.add_status_listener(self.reset_listener)

    if controller.is_alive():
      self.reset_listener(controller, State.RESET, None)

  def reset_listener(self, controller, event_type, _):
    """
    Reloads and displays the torrc on tor reload (sighup) events.
    """

    if event_type == State.RESET:
      self.torrc_location = expand_path(controller.get_info('config-file', None))

      try:
        with open(self.torrc_location) as torrc_file:
          self.torrc_content = torrc_file.readlines()
      except IOError as exc:
        log.log_once('torrc_load_failed', log.WARN, 'Unable to load torrc (%s)' % exc.strerror)
        self.torrc_content = None

  def set_comments_visible(self, is_visible):
    """
    Sets if comments and blank lines are shown or stripped.

    Arguments:
      is_visible - displayed comments and blank lines if true, strips otherwise
    """

    self.strip_comments = not is_visible
    self._last_content_height_args = None
    self.redraw(True)

  def set_line_number_visible(self, is_visible):
    """
    Sets if line numbers are shown or hidden.

    Arguments:
      is_visible - displays line numbers if true, hides otherwise
    """

    self.show_line_num = is_visible
    self._last_content_height_args = None
    self.redraw(True)

  def handle_key(self, key):
    if key.is_scroll():
      page_height = self.get_preferred_size()[0] - 1
      new_scroll = ui_tools.get_scroll_position(key, self.scroll, page_height, self._last_content_height)

      if self.scroll != new_scroll:
        self.scroll = new_scroll
        self.redraw(True)
    elif key.match('n'):
      self.set_line_number_visible(not self.show_line_num)
    elif key.match('s'):
      self.set_comments_visible(self.strip_comments)
    else:
      return False

    return True

  def set_visible(self, is_visible):
    if not is_visible:
      self._last_content_height_args = None  # redraws when next displayed

    panel.Panel.set_visible(self, is_visible)

  def get_help(self):
    return [
      ('up arrow', 'scroll up a line', None),
      ('down arrow', 'scroll down a line', None),
      ('page up', 'scroll up a page', None),
      ('page down', 'scroll down a page', None),
      ('s', 'comment stripping', 'on' if self.strip_comments else 'off'),
      ('n', 'line numbering', 'on' if self.show_line_num else 'off'),
      ('x', 'reset tor (issue sighup)', None),
    ]

  def draw(self, width, height):
    # If true, we assume that the cached value in self._last_content_height is
    # still accurate, and stop drawing when there's nothing more to display.
    # Otherwise the self._last_content_height is suspect, and we'll process all
    # the content to check if it's right (and redraw again with the corrected
    # height if not).

    trust_last_content_height = self._last_content_height_args == (width, height)

    # restricts scroll location to valid bounds

    self.scroll = max(0, min(self.scroll, self._last_content_height - height + 1))

    if self.torrc_content is None:
      rendered_contents = ['### Unable to load the torrc ###']
    else:
      rendered_contents = [ui_tools.get_printable(line.replace('\t', '   ')) for line in self.torrc_content]

      if self.strip_comments:
        for i in range(len(rendered_contents)):
          line = rendered_contents[i]

          if line and '#' in line:
            rendered_contents[i] = line[:line.find('#')].strip()

    # offset to make room for the line numbers

    line_number_offset = 0

    if self.show_line_num:
      if len(rendered_contents) == 0:
        line_number_offset = 2
      else:
        line_number_offset = int(math.log10(len(rendered_contents))) + 2

    # draws left-hand scroll bar if content's longer than the height

    scroll_offset = 0

    if self._last_content_height > height - 1:
      scroll_offset = 3
      self.add_scroll_bar(self.scroll, self.scroll + height - 1, self._last_content_height, 1)

    display_line = -self.scroll + 1  # line we're drawing on

    # draws the top label

    if self.is_title_visible():
      location_label = ' (%s)' % self.torrc_location
      self.addstr(0, 0, 'Tor Configuration File%s:' % (location_label), curses.A_STANDOUT)

    is_multiline = False  # true if we're in the middle of a multiline torrc entry

    for line_number in range(0, len(rendered_contents)):
      line_text = rendered_contents[line_number]
      line_text = line_text.rstrip()  # remove ending whitespace

      # blank lines are hidden when stripping comments

      if self.strip_comments and not line_text:
        continue

      # splits the line into its component (msg, format) tuples

      line_comp = {
        'option': ['', (curses.A_BOLD, 'green')],
        'argument': ['', (curses.A_BOLD, 'cyan')],
        'correction': ['', (curses.A_BOLD, 'cyan')],
        'comment': ['', ('white',)],
      }

      # parses the comment

      comment_index = line_text.find('#')

      if comment_index != -1:
        line_comp['comment'][0] = line_text[comment_index:]
        line_text = line_text[:comment_index]

      # splits the option and argument, preserving any whitespace around them

      stripped_line = line_text.strip()
      option_index = stripped_line.find(' ')

      if is_multiline:
        # part of a multiline entry started on a previous line so everything
        # is part of the argument
        line_comp['argument'][0] = line_text
      elif option_index == -1:
        # no argument provided
        line_comp['option'][0] = line_text
      else:
        option_text = stripped_line[:option_index]
        option_end = line_text.find(option_text) + len(option_text)
        line_comp['option'][0] = line_text[:option_end]
        line_comp['argument'][0] = line_text[option_end:]

      # flags following lines as belonging to this multiline entry if it ends
      # with a slash

      if stripped_line:
        is_multiline = stripped_line.endswith('\\')

      # draws the line number

      if self.show_line_num and display_line < height and display_line >= 1:
        line_number_str = ('%%%ii' % (line_number_offset - 1)) % (line_number + 1)
        self.addstr(display_line, scroll_offset, line_number_str, curses.A_BOLD, 'yellow')

      # draws the rest of the components with line wrap

      cursor_location, line_offset = line_number_offset + scroll_offset, 0
      display_queue = [line_comp[entry] for entry in ('option', 'argument', 'correction', 'comment')]

      while display_queue:
        msg, format = display_queue.pop(0)

        max_msg_size, include_break = width - cursor_location, False

        if len(msg) >= max_msg_size:
          # message is too long - break it up

          if line_offset == MAX_WRAP_PER_LINE - 1:
            msg = str_tools.crop(msg, max_msg_size)
          else:
            include_break = True
            msg, remainder = str_tools.crop(msg, max_msg_size, 4, 4, str_tools.Ending.HYPHEN, True)
            display_queue.insert(0, (remainder.strip(), format))

        draw_line = display_line + line_offset

        if msg and draw_line < height and draw_line >= 1:
          self.addstr(draw_line, cursor_location, msg, *format)

        # If we're done, and have added content to this line, then start
        # further content on the next line.

        cursor_location += len(msg)
        include_break |= not display_queue and cursor_location != line_number_offset + scroll_offset

        if include_break:
          line_offset += 1
          cursor_location = line_number_offset + scroll_offset

      display_line += max(line_offset, 1)

      if trust_last_content_height and display_line >= height:
        break

    if not trust_last_content_height:
      self._last_content_height_args = (width, height)
      new_content_height = display_line + self.scroll - 1

      if self._last_content_height != new_content_height:
        self._last_content_height = new_content_height
        self.redraw(True)
