/**
 * Order service module.
 */

import { Base } from "./base";

/** Adds two numbers. */
export function add(a: number, b: number): number {
  return a + b;
}

/**
 * Handles orders.
 * @public
 */
export class OrderService extends Base {
  /** Place an order. */
  async place(id: string): Promise<void> {
    const check = (x: string) => x.length > 0;
    check(id);
  }

  get count(): number {
    return 0;
  }
}

/** Doubles a number. */
export const double = (n: number): number => n * 2;
