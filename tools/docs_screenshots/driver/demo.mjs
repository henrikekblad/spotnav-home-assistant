// The demo's car identification, shared with the app's screenshot tool: the camera and its reference pictures,
// and plug-ins that show "identifying", a car decided by its charging cable, and the question.
//
// Order: `setupCamera` and `decideFamilyCar` once the charger exists; later `unplug`, then `plugIn` (it waits
// until the unplug counts, about two minutes after it), `decideCityCar` and `askBetweenBoth`.
//
// Everything goes through the demo integrations' own screenshot-only services (`ocpp.docs_set_status`,
// `kia_uvo.docs_set_plug`, `demo_vision.docs_park`) and SpotNav's public WebSocket commands.
import { sleep } from "./ha.mjs";

export const CAMERA = "camera.driveway_camera";
export const AI_TASK = "ai_task.local_vision_model";
/** The parking bay in the drawn camera picture, as fractions of its width and height. */
export const FRAME = { x: 0.24, y: 0.36, w: 0.52, h: 0.62 };
/** A short unplug is the same plug-in (SpotNav's `UNPLUG_DEBOUNCE_S` is 120 s): a new one needs a longer one. */
export const UNPLUG_SETTLE_MS = 130_000;
/** The demo Kia cars by name, as their entity ids and the services know them. */
const PREFIX = { "Family car": "family_car", "City car": "city_car" };

const service = (api, domain, name, data) => api.post(`/api/services/${domain}/${name}`, data);

export const setConnector = (api, status) => service(api, "ocpp", "docs_set_status", { status });
export const setPlug = (api, name, plugged) => service(api, "kia_uvo", "docs_set_plug", { vehicle: PREFIX[name], plugged });
export const park = (api, name) => service(api, "demo_vision", "docs_park", { vehicle: name === null ? "none" : PREFIX[name] });

export const dashboard = (api, chargerId) => api.ws({ type: "spotnav/get_dashboard", api_version: 1, charger_id: chargerId });

async function identification(api, chargerId) {
  return (await dashboard(api, chargerId)).identification ?? null;
}

/** Wait until the charger's identification `test(block)` holds; the block it ended with. */
export async function waitIdentification(api, chargerId, what, test, timeoutS = 60) {
  let block = null;
  for (let i = 0; i < timeoutS * 2; i++) {
    block = await identification(api, chargerId);
    if (test(block)) return block;
    await sleep(500);
  }
  throw new Error(`identification never became ${what}: ${JSON.stringify(block).slice(0, 400)}`);
}

/** Replace the charger's settings with `change(body)` applied, as a client does (the full record, its revision). */
export async function updateSettings(api, chargerId, change) {
  const current = (await api.ws({ type: "spotnav/get_settings", api_version: 1, charger_id: chargerId })).settings;
  const { revision, ...body } = current;
  change(body);
  const r = await api.ws({ type: "spotnav/update_settings", api_version: 1, charger_id: chargerId, expected_revision: revision, settings: body });
  if (r.ok === false) throw new Error("settings refused: " + JSON.stringify(r).slice(0, 300));
  return r;
}

/**
 * The camera, its AI task and the parking bay's frame, and a day reference picture of each car, taken of the
 * drawn picture with that car parked. "Family car" is left standing in the picture.
 */
export async function setupCamera(api, chargerId) {
  await updateSettings(api, chargerId, (body) => {
    body.identify_camera = { camera_entity_id: CAMERA, ai_task_entity_id: AI_TASK, frame: FRAME };
  });
  const cars = (await dashboard(api, chargerId)).vehicles ?? [];
  for (const car of cars) {
    if (!(car.name in PREFIX)) continue;
    await park(api, car.name);
    const r = await api.ws({ type: "spotnav/take_reference_picture", api_version: 1, charger_id: chargerId, vehicle_id: car.id, kind: "day" });
    if (!r.ok) throw new Error(`reference picture of ${car.name}: ${JSON.stringify(r)}`);
  }
  await park(api, "Family car");
}

/** The plug-in SpotNav sees when the charger is added: "Family car" says it is plugged in, so it is the car. */
export async function decideFamilyCar(api, chargerId) {
  await waitIdentification(api, chargerId, "started", (b) => b !== null && b.state != null);
  await setPlug(api, "Family car", true);
  return waitIdentification(api, chargerId, "decided", (b) => b?.state === "decided");
}

/** Unplug, with both cars' cables off. Returns when, for `plugIn`. */
export async function unplug(api) {
  await setConnector(api, "Available");
  await setPlug(api, "Family car", false);
  await setPlug(api, "City car", false);
  return Date.now();
}

/** Plug in again once the unplug at `unpluggedAt` counts as one: SpotNav identifies, and nothing has decided yet. */
export async function plugIn(api, chargerId, unpluggedAt) {
  const wait = unpluggedAt + UNPLUG_SETTLE_MS - Date.now();
  if (wait > 0) {
    console.log(`== waiting ${Math.ceil(wait / 1000)} s for the unplug to count`);
    await sleep(wait);
  }
  await setConnector(api, "Preparing");
  return waitIdentification(api, chargerId, "identifying", (b) => b?.state === "waiting");
}

/** "City car" says it was plugged in: it is the car, decided by its charging cable. */
export async function decideCityCar(api, chargerId) {
  await setPlug(api, "City car", true);
  return waitIdentification(api, chargerId, "decided", (b) => b?.state === "decided");
}

/**
 * A quick replug (within SpotNav's two minutes) with "Family car" saying it is plugged in too: another car than
 * the decided one may be here now, and with both cars saying so SpotNav asks at once. Call within five minutes of
 * `decideCityCar`, while "City car"'s report still counts as fresh.
 */
export async function askBetweenBoth(api, chargerId) {
  await setConnector(api, "Available");
  await sleep(1500);
  await setPlug(api, "Family car", true);
  await sleep(1500);
  await setConnector(api, "Preparing");
  return waitIdentification(api, chargerId, "asking", (b) => b?.state === "asking");
}
