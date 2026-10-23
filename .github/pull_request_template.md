<!-- 中文也可以。/ Chinese is fine. -->

**What this changes, and why the obvious alternative was worse**

**What you actually verified**
<!-- "Tests pass" is fine if they do. "I could not test the APNs path, no device"
     is far more useful than silence.
     寫你真的驗過什麼。「沒測 APNs，沒有裝置」比不講有用得多。 -->

- [ ] `./scripts/run-tests.sh` — result:
- [ ] New behaviour has a test, or I said why it cannot have one
- [ ] No credentials, tailnet names, personal hostnames or `/Users/<me>` paths
      <!-- `tests/test_no_private_infra_leak.py` matches by shape, so it will
           also catch *your* tailnet if you pasted debug output. -->
