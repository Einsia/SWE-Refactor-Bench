import jsonata = require('jsonata');
const e = jsonata('$x');
const p: Promise<unknown> = e.evaluate({}, { x: 1 });
void p;
