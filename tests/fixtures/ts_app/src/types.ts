/** Something with an area. */
export interface HasArea {
  area(): number;
}

/** A shape. */
export abstract class Shape implements HasArea {
  abstract area(): number;

  describe(): string {
    return `area ${this.area()}`;
  }
}

/** An identifier. */
export type Id<T> = string | T;

export enum Color {
  Red,
  Green = "g",
}
