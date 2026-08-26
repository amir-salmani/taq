/* taq · second clock — a second timezone beside the GNOME top-bar clock.
 *
 * GNOME can show world clocks inside the calendar popover, but nothing in the
 * panel itself. When the people you work with are in one country and the people
 * you call are in another, the useful place for that number is the one you
 * glance at without deciding to.
 *
 * No timer: the panel clock already ticks once a minute, so this follows its
 * label instead of scheduling anything of its own.
 */

import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const DEFAULT_ZONE = 'Europe/Helsinki';
const ZONE_FILE = GLib.build_filenamev(
    [GLib.get_user_config_dir(), 'taq', 'second-zone']);

export default class TaqSecondClock extends Extension {
    enable() {
        const dateMenu = Main.panel.statusArea?.dateMenu;
        this._clock = dateMenu?._clockDisplay;
        if (!this._clock) {
            // _clockDisplay is private shell API. Every world-clock extension
            // leans on it; if a future release renames it, fail visibly in the
            // log rather than silently doing nothing.
            console.warn('taq-clock: dateMenu._clockDisplay not found, giving up');
            return;
        }

        this._tz = this._resolveZone();
        this._interface = new Gio.Settings({schema_id: 'org.gnome.desktop.interface'});
        this._formatId = this._interface.connect('changed::clock-format',
            () => this._sync());

        this._label = new St.Label({
            style_class: 'taq-second-clock',
            y_align: Clutter.ActorAlign.CENTER,
        });
        this._clock.get_parent().insert_child_above(this._label, this._clock);

        this._notifyId = this._clock.connect('notify::text', () => this._sync());
        this._sync();
    }

    disable() {
        if (this._notifyId) {
            this._clock.disconnect(this._notifyId);
            this._notifyId = null;
        }
        if (this._formatId) {
            this._interface.disconnect(this._formatId);
            this._formatId = null;
        }
        this._label?.destroy();
        this._label = null;
        this._clock = null;
        this._interface = null;
        this._tz = null;
    }

    /* Zone name from ~/.config/taq/second-zone when present, so this and taq
     * itself can be pointed at the same place. */
    _resolveZone() {
        let name = DEFAULT_ZONE;
        try {
            const [ok, bytes] = GLib.file_get_contents(ZONE_FILE);
            if (ok) {
                const text = new TextDecoder().decode(bytes).trim();
                if (text)
                    name = text;
            }
        } catch {
            // no override file; the default stands
        }
        return GLib.TimeZone.new_identifier(name) ?? GLib.TimeZone.new_utc();
    }

    _sync() {
        if (!this._label)
            return;
        const fmt = this._interface.get_string('clock-format') === '12h'
            ? '%l:%M %p' : '%H:%M';
        const now = GLib.DateTime.new_now(this._tz);
        this._label.text = ` | ${now.format(fmt).trim()}`;
    }
}
