import jsonata = require('jsonata');
// @expect-error JsonataOptions is closed over its five documented keys
jsonata('1', { nosuchoption: 1 });
