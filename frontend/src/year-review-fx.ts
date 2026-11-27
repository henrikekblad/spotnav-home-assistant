// The year review's motion: the big figure counted up (900 ms, ease-out) and fireworks on a canvas behind the cover
// and the thanks page (rockets that rise and burst into sparks). Under `prefers-reduced-motion` neither runs: the
// figure is written at once and no frame is ever asked for. Every loop is stopped when the story closes, the page
// changes or the tab is hidden (`year-review-view.ts`).

export const COUNT_UP_MS = 900;
const LAUNCH_EVERY_MS = 650;
const SPARKS = 46;
const COLOURS = ["#ffe066", "#ec6fb5", "#7ff0de", "#9ab8ff", "#ffffff", "#ffb36b"];

/** The frame clock a loop runs on: the window's, replaced in the tests. */
export interface FrameClock {
  request(callback: (time: number) => void): number;
  cancel(handle: number): void;
  now(): number;
}

export function windowClock(view: Window): FrameClock {
  return {
    request: (callback) => view.requestAnimationFrame(callback),
    cancel: (handle) => view.cancelAnimationFrame(handle),
    now: () => view.performance.now(),
  };
}

/** Whether the reader asked for less motion (no `matchMedia`: as if not). */
export function reducedMotion(view: Window | null): boolean {
  try {
    return view?.matchMedia?.("(prefers-reduced-motion: reduce)").matches === true;
  } catch {
    return false;
  }
}

/** Cubic ease-out over `[0, 1]`. */
export function easeOut(progress: number): number {
  const k = Math.min(1, Math.max(0, progress));
  return 1 - (1 - k) ** 3;
}

/**
 * Count `target` up into `node`'s text from zero, each frame's value written by `format`; the final value at once
 * under reduced motion. The returned function stops it (the final value is then written).
 */
export function countUp(
  node: Text,
  target: number,
  format: (value: number) => string,
  clock: FrameClock | null,
  reduced: boolean,
): () => void {
  node.data = format(target);
  if (reduced || clock === null || !(Math.abs(target) > 0)) {
    return () => {};
  }
  const start = clock.now();
  let handle: number | null = null;
  const step = (time: number): void => {
    const progress = (time - start) / COUNT_UP_MS;
    node.data = format(progress >= 1 ? target : target * easeOut(progress));
    handle = progress >= 1 ? null : clock.request(step);
  };
  node.data = format(0);
  handle = clock.request(step);
  return () => {
    if (handle !== null) {
      clock.cancel(handle);
      handle = null;
      node.data = format(target);
    }
  };
}

interface Rocket {
  x: number;
  y: number;
  vy: number;
  colour: string;
}

interface Spark {
  x: number;
  y: number;
  vx: number;
  vy: number;
  life: number;
  colour: string;
}

/** Fireworks on one canvas: `start()` and `stop()`; nothing at all under reduced motion or without a 2D context. */
export class Fireworks {
  private readonly canvas: HTMLCanvasElement;
  private readonly clock: FrameClock | null;
  private readonly reduced: boolean;
  private readonly random: () => number;
  private context: CanvasRenderingContext2D | null = null;
  private handle: number | null = null;
  private rockets: Rocket[] = [];
  private sparks: Spark[] = [];
  private lastLaunch = -Infinity;
  private width = 0;
  private height = 0;

  constructor(canvas: HTMLCanvasElement, clock: FrameClock | null, reduced: boolean, random: () => number = Math.random) {
    this.canvas = canvas;
    this.clock = clock;
    this.reduced = reduced;
    this.random = random;
  }

  get running(): boolean {
    return this.handle !== null;
  }

  start(): void {
    if (this.reduced || this.clock === null || this.handle !== null) {
      return;
    }
    let context: CanvasRenderingContext2D | null = null;
    try {
      context = this.canvas.getContext("2d");
    } catch {
      context = null;
    }
    if (context === null) {
      return;
    }
    this.context = context;
    const box = this.canvas.getBoundingClientRect();
    const ratio = this.canvas.ownerDocument.defaultView?.devicePixelRatio ?? 1;
    this.width = box.width;
    this.height = box.height;
    this.canvas.width = Math.max(1, Math.round(box.width * ratio));
    this.canvas.height = Math.max(1, Math.round(box.height * ratio));
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    this.lastLaunch = -Infinity;
    this.handle = this.clock.request((time) => this.frame(time));
  }

  stop(): void {
    if (this.handle !== null) {
      this.clock?.cancel(this.handle);
      this.handle = null;
    }
    this.rockets = [];
    this.sparks = [];
    this.context?.clearRect(0, 0, this.width, this.height);
  }

  private frame(time: number): void {
    const context = this.context;
    if (context === null || this.handle === null) {
      return;
    }
    context.clearRect(0, 0, this.width, this.height);
    if (time - this.lastLaunch > LAUNCH_EVERY_MS) {
      this.rockets.push({
        x: this.width * (0.15 + this.random() * 0.7),
        y: this.height,
        vy: -(5.5 + this.random() * 2.5),
        colour: COLOURS[Math.floor(this.random() * COLOURS.length)] ?? "#ffffff",
      });
      this.lastLaunch = time;
    }
    this.rockets = this.rockets.filter((rocket) => {
      rocket.y += rocket.vy;
      rocket.vy += 0.07;
      context.fillStyle = rocket.colour;
      context.fillRect(rocket.x - 1.5, rocket.y, 3, 7);
      if (rocket.vy > -1.2) {
        for (let i = 0; i < SPARKS; i += 1) {
          const angle = this.random() * Math.PI * 2;
          const speed = 1 + this.random() * 3.2;
          this.sparks.push({ x: rocket.x, y: rocket.y, vx: Math.cos(angle) * speed, vy: Math.sin(angle) * speed, life: 1, colour: rocket.colour });
        }
        return false;
      }
      return true;
    });
    this.sparks = this.sparks.filter((spark) => {
      spark.x += spark.vx;
      spark.y += spark.vy;
      spark.vy += 0.045;
      spark.vx *= 0.985;
      spark.life -= 0.014;
      context.globalAlpha = Math.max(0, spark.life);
      context.fillStyle = spark.colour;
      context.beginPath();
      context.arc(spark.x, spark.y, 1.8, 0, Math.PI * 2);
      context.fill();
      context.globalAlpha = 1;
      return spark.life > 0;
    });
    this.handle = this.clock?.request((next) => this.frame(next)) ?? null;
  }
}
