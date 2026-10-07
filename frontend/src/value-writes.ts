// The writes one value of the Settings page makes, as the view hands them to the card. Each goes through the
// same request, validation and conflict handling as the dialog it replaces; the card answers `null` when it
// took, or the sentence to show in the editor.

import type { VehicleChanges } from "./api";
import type { EntityDraft, EntityScope } from "./entity-config";
import type { ReplacementCheck } from "./settings";
import type { CameraFrame, SettingsRecord } from "./types";
import type { Vehicle } from "./validate";

export type FiscalComponentName = "vat" | "tax" | "transfer";

export type ValueWrite =
  /**
   * A vehicle's own property (`spotnav/update_vehicle`, compare-and-set on what the row showed). On a conflict
   * the answer's row goes to `onConflict`, so the next Save expects what is stored now.
   */
  | {
      kind: "vehicle";
      vehicleId: string;
      changes: VehicleChanges["changes"];
      expected: VehicleChanges["expected"];
      onConflict?: (row: Vehicle) => void;
    }
  /** A vehicle's charge-level sensor (`spotnav/choose_vehicle_soc`; `null` is automatic). */
  | { kind: "vehicleSoc"; vehicleId: string; entityId: string | null }
  /** A car's own charge limit (`spotnav/write_charge_limit`), written through the car's integration. */
  | { kind: "chargeLimit"; vehicleId: string; percent: number }
  /** A vehicle's plug or location source (`spotnav/choose_vehicle_identification`). */
  | { kind: "vehicleSource"; vehicleId: string; source: "plug" | "location"; entityId: string | null }
  /** One field of the charger's or the site's entity configuration (`spotnav/update_entity_config`). */
  | { kind: "entity"; scope: EntityScope; draft: EntityDraft }
  /** The site's solar priority or forecast sources (`spotnav/update_site_settings`). */
  | { kind: "solar"; priority?: string; forecast?: string[] }
  /** A settings replacement built from a freshly read record (`spotnav/update_settings`). */
  | { kind: "settings"; build: (record: SettingsRecord) => ReplacementCheck }
  /** The camera's frame (`spotnav/save_camera_frame`; `null` is the whole picture). */
  | { kind: "cameraFrame"; frame: CameraFrame | null }
  /** One fee of the selected area: a figure, or `null` for off. */
  | { kind: "fiscal"; component: FiscalComponentName; value: number | null };
