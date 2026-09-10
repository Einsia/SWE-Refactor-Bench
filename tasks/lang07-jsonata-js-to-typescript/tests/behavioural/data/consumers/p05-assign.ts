import jsonata = require('jsonata');
const e = jsonata('$x');
e.assign('x', 1);
e.assign('y', { nested: [1, 2] });
