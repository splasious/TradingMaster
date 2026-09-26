/** What the site shows. Delta Exchange is hidden: its saved data stays on
 * the server, but no page offers it (the backend also refuses and leaves it
 * out -- backend/app/services/visibility.py). Set true, together with
 * SHOW_DELTA_EXCHANGE=true in the backend's .env, to bring it back. */
export const DELTA_VISIBLE = false;
