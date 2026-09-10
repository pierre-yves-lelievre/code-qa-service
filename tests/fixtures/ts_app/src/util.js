#!/usr/bin/env node
/** Utilities for scripts. */

function* ids() {
  yield 1;
}

class Cache {
  get(key) {
    return key;
  }
}

const load = function () {
  return <div />;
};
