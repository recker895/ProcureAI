const path=require('node:path');
module.exports={testDir:__dirname,testMatch:'browser.spec.cjs',workers:1,timeout:30000,reporter:'list',outputDir:path.join(__dirname,'../evidence/browser-results'),use:{baseURL:'http://127.0.0.1:8766',headless:true,channel:process.env.PROCURE_TEST_BROWSER||'msedge',reducedMotion:'reduce',viewport:{width:1440,height:1000}}};
