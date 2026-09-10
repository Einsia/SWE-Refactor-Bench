import jsonata = require('jsonata');
// @expect-error recover is a boolean
jsonata('1', { recover: 'yes' });
