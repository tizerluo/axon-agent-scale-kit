import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

import sys

sys.path.append(str(Path(__file__).resolve().parents[1] / "scripts"))
import axonctl


class AxonCtlRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.network_file = self.base / "network.yaml"
        self.agents_file = self.base / "agents.yaml"
        self.state_file = self.base / "state.json"
        self.hosts_file = self.base / "hosts.yaml"
        self.network_file.write_text(
            yaml.safe_dump(
                {
                    "rpc_url": "https://mainnet-rpc.axonchain.ai/",
                    "evm_chain_id": 8210,
                    "cosmos_chain_id": "axon_8210-1",
                    "deploy": {"default_concurrency": 2, "retry_times": 1},
                },
                sort_keys=False,
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        self.agents_file.write_text(
            yaml.safe_dump(
                {
                    "agents": [
                        {"name": "agent-001", "wallet_ref": "KEY_001"},
                        {"name": "agent-002", "wallet_ref": "KEY_002"},
                        {"name": "agent-003", "wallet_ref": "KEY_003"},
                    ]
                },
                sort_keys=False,
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        self.hosts_file.write_text(
            yaml.safe_dump(
                {
                    "hosts": [
                        {
                            "name": "test-host",
                            "host": "10.0.0.1",
                            "user": "root",
                            "ssh_key": "/tmp/test.pem",
                            "workdir": "/opt/axon-agent-scale",
                            "python_bin": "python3",
                            "use_sudo": False,
                        }
                    ]
                },
                sort_keys=False,
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        self.valid_address = "0x1111111111111111111111111111111111111111"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @mock.patch("axonctl.rpc_chain_id", return_value=(True, 8210, None))
    def test_validate_passes_with_standard_network(self, _rpc_mock: mock.Mock) -> None:
        self.assertEqual(axonctl.validate(str(self.network_file), str(self.agents_file), strict_rpc=True), 0)

    def test_render_service_unit_contains_execstart(self) -> None:
        unit = axonctl.render_service_unit(
            service_name="axon-agent-agent-001.service",
            agent_name="agent-001",
            remote_workdir="/opt/axon-agent-scale",
            python_bin="python3",
        )
        self.assertIn("ExecStart=python3 /opt/axon-agent-scale/scripts/agent_worker.py --agent agent-001", unit)

    def test_funding_wallet_template_and_import(self) -> None:
        wallet_file = self.base / "funding_wallet.template.yaml"
        self.assertEqual(axonctl.funding_wallet_template(str(wallet_file)), 0)
        data = yaml.safe_load(wallet_file.read_text(encoding="utf-8"))
        data["address"] = self.valid_address
        data["private_key"] = "a" * 64
        wallet_file.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
        self.assertEqual(axonctl.funding_wallet_import(str(self.state_file), str(wallet_file)), 0)
        state = axonctl.load_state(str(self.state_file))
        self.assertEqual(state["settings"]["funding_address"], self.valid_address)

    def test_agent_wallet_template_and_import(self) -> None:
        from eth_account import Account

        wallet_file = self.base / "agent_wallet.template.yaml"
        self.assertEqual(axonctl.agent_wallet_template(str(wallet_file)), 0)
        data = yaml.safe_load(wallet_file.read_text(encoding="utf-8"))
        pk = "1" * 64
        addr = Account.from_key(f"0x{pk}").address
        data["name"] = "agent-legacy-001"
        data["private_key"] = pk
        data["address"] = addr
        wallet_file.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
        self.assertEqual(
            axonctl.agent_wallet_import(
                state_file=str(self.state_file),
                agent_name=data["name"],
                private_key=data["private_key"],
                address=data["address"],
                mnemonic="",
                overwrite=False,
            ),
            0,
        )
        state = axonctl.load_state(str(self.state_file))
        self.assertEqual(state["agents"]["agent-legacy-001"]["wallet_address"], addr)
        self.assertEqual(state["agents"]["agent-legacy-001"]["container_name"], "axon-agent-agent-legacy-001")
        found = [w for w in state["wallets"].values() if w.get("label") == "agent:agent-legacy-001"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["address"], addr)

    def test_agent_wallets_import_batch(self) -> None:
        from eth_account import Account

        batch_file = self.base / "agent_wallets.yaml"
        pk1 = "2" * 64
        pk2 = "3" * 64
        addr1 = Account.from_key(f"0x{pk1}").address
        addr2 = Account.from_key(f"0x{pk2}").address
        batch_file.write_text(
            yaml.safe_dump(
                {
                    "agents": [
                        {"name": "agent-legacy-002", "private_key": pk1, "address": addr1},
                        {"name": "agent-legacy-003", "private_key": pk2, "address": addr2},
                    ]
                },
                sort_keys=False,
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        self.assertEqual(axonctl.agent_wallets_import(str(self.state_file), str(batch_file), overwrite=False), 0)
        state = axonctl.load_state(str(self.state_file))
        self.assertEqual(state["agents"]["agent-legacy-002"]["wallet_address"], addr1)
        self.assertEqual(state["agents"]["agent-legacy-003"]["wallet_address"], addr2)
        self.assertEqual(state["agents"]["agent-legacy-002"]["container_name"], "axon-agent-agent-legacy-002")
        self.assertEqual(state["agents"]["agent-legacy-003"]["container_name"], "axon-agent-agent-legacy-003")

    def test_agent_wallet_import_reused_sets_container_name(self) -> None:
        from eth_account import Account

        pk = "1" * 64
        addr = Account.from_key(f"0x{pk}").address
        self.assertEqual(
            axonctl.agent_wallet_import(
                state_file=str(self.state_file),
                agent_name="agent-001",
                private_key=pk,
                address=addr,
                mnemonic="",
                overwrite=False,
            ),
            0,
        )
        state = axonctl.load_state(str(self.state_file))
        self.assertEqual(state["agents"]["agent-001"]["container_name"], "axon-agent-agent-001")
        self.assertEqual(
            axonctl.agent_wallet_import(
                state_file=str(self.state_file),
                agent_name="agent-001",
                private_key=pk,
                address=addr,
                mnemonic="",
                overwrite=True,
            ),
            0,
        )
        state2 = axonctl.load_state(str(self.state_file))
        self.assertEqual(state2["agents"]["agent-001"]["container_name"], "axon-agent-agent-001")

    def test_request_create_rejects_insufficient_min_funding(self) -> None:
        code = axonctl.create_request(
            state_file=str(self.state_file),
            target_agents=2,
            min_funding_axon=150.0,
            funding_address=self.valid_address,
            min_confirmations=2,
            timeout_sec=600,
            stake_per_agent_axon=100.0,
        )
        self.assertEqual(code, 1)

    @mock.patch("axonctl.rpc_chain_id", return_value=(True, 8210, None))
    @mock.patch(
        "axonctl._register_agent_onchain",
        return_value=(
            True,
            {
                "status": "registered_onchain",
                "registration": {
                    "status": "registered_onchain",
                    "tx_hash": "0x123",
                    "receipt_status": 1,
                    "block_number": 100,
                    "from": "0x2222222222222222222222222222222222222222",
                    "to": axonctl.REGISTRY_PRECOMPILE,
                    "value_axon": 100.0,
                    "method": axonctl.REGISTER_METHOD_SIGNATURE,
                    "burn_expected_axon": 20,
                    "evidence_mode": "register_payable_path_proof",
                    "post_check": {"is_agent": True, "agent_id": "agent-x", "reputation": 10, "is_online": True},
                },
            },
        ),
    )
    @mock.patch(
        "axonctl._ensure_agent_wallet",
        return_value={
            "key_id": "testkey",
            "address": "0x2222222222222222222222222222222222222222",
            "private_key": "0x" + "a" * 64,
        },
    )
    def test_funded_plan_scale_repair_status_flow(self, _wallet_mock: mock.Mock, _register_mock: mock.Mock, _rpc_mock: mock.Mock) -> None:
        self.assertEqual(
            axonctl.create_request(
                state_file=str(self.state_file),
                target_agents=2,
                min_funding_axon=250.0,
                funding_address=self.valid_address,
                min_confirmations=2,
                timeout_sec=600,
                stake_per_agent_axon=100.0,
            ),
            0,
        )
        request_id = next(iter(axonctl.load_state(str(self.state_file))["requests"]))
        self.assertEqual(
            axonctl.fund_check(
                state_file=str(self.state_file),
                network=str(self.network_file),
                request_id=request_id,
                observed_amount_axon=250.0,
                observed_confirmations=3,
                observed_chain_id=8210,
                strict_rpc=True,
            ),
            0,
        )
        self.assertEqual(axonctl.build_scale_plan(str(self.state_file), str(self.network_file), str(self.agents_file), request_id), 0)
        self.assertEqual(axonctl.execute_scale(str(self.state_file), str(self.network_file), str(self.agents_file), request_id, ["agent-002"]), 0)
        after_scale = axonctl.load_state(str(self.state_file))
        self.assertIn("agent-002", after_scale["requests"][request_id]["execution"]["failed_agents"])
        self.assertEqual(axonctl.repair(str(self.state_file), request_id), 0)

    @mock.patch("axonctl.rpc_chain_id", return_value=(True, 8210, None))
    @mock.patch(
        "axonctl._register_agent_onchain",
        return_value=(
            True,
            {
                "status": "registered_onchain",
                "registration": {
                    "status": "registered_onchain",
                    "tx_hash": "0xabc1",
                    "receipt_status": 1,
                    "block_number": 101,
                    "from": "0x2222222222222222222222222222222222222222",
                    "to": axonctl.REGISTRY_PRECOMPILE,
                    "value_axon": 100.0,
                    "method": axonctl.REGISTER_METHOD_SIGNATURE,
                    "burn_expected_axon": 20,
                    "evidence_mode": "register_payable_path_proof",
                    "post_check": {"is_agent": True, "agent_id": "agent-y", "reputation": 10, "is_online": True},
                },
            },
        ),
    )
    @mock.patch(
        "axonctl._ensure_agent_wallet",
        return_value={
            "key_id": "testkey",
            "address": "0x2222222222222222222222222222222222222222",
            "private_key": "0x" + "a" * 64,
        },
    )
    def test_run_intent_pipeline_success(self, _wallet_mock: mock.Mock, _register_mock: mock.Mock, _rpc_mock: mock.Mock) -> None:
        self.assertEqual(axonctl.funding_wallet_set(str(self.state_file), self.valid_address), 0)
        code = axonctl.run_intent_pipeline(
            state_file=str(self.state_file),
            network=str(self.network_file),
            agents=str(self.agents_file),
            intent="I fund 250 AXON, scale 2 agents",
            funding_address=None,
            observed_confirmations=3,
            observed_chain_id=8210,
            strict_rpc=True,
        )
        self.assertEqual(code, 0)

    @mock.patch("axonctl.rpc_chain_id", return_value=(True, 8210, None))
    @mock.patch(
        "axonctl._register_agent_onchain",
        return_value=(
            True,
            {
                "status": "registered_onchain",
                "registration": {
                    "status": "registered_onchain",
                    "tx_hash": "0xabc2",
                    "receipt_status": 1,
                    "block_number": 102,
                    "from": "0x2222222222222222222222222222222222222222",
                    "to": axonctl.REGISTRY_PRECOMPILE,
                    "value_axon": 100.0,
                    "method": axonctl.REGISTER_METHOD_SIGNATURE,
                    "burn_expected_axon": 20,
                    "evidence_mode": "register_payable_path_proof",
                    "post_check": {"is_agent": True, "agent_id": "agent-z", "reputation": 10, "is_online": True},
                },
            },
        ),
    )
    @mock.patch(
        "axonctl._ensure_agent_wallet",
        return_value={
            "key_id": "testkey",
            "address": "0x2222222222222222222222222222222222222222",
            "private_key": "0x" + "a" * 64,
        },
    )
    @mock.patch("axonctl.scp_to", return_value=(True, "", ""))
    @mock.patch("axonctl.run_ssh")
    def test_remote_deploy_and_remote_status(
        self,
        ssh_mock: mock.Mock,
        _scp_mock: mock.Mock,
        _wallet_mock: mock.Mock,
        _register_mock: mock.Mock,
        _rpc_mock: mock.Mock,
    ) -> None:
        self.assertEqual(
            axonctl.create_request(
                state_file=str(self.state_file),
                target_agents=2,
                min_funding_axon=250.0,
                funding_address=self.valid_address,
                min_confirmations=2,
                timeout_sec=600,
                stake_per_agent_axon=100.0,
            ),
            0,
        )
        request_id = next(iter(axonctl.load_state(str(self.state_file))["requests"]))
        self.assertEqual(
            axonctl.fund_check(
                state_file=str(self.state_file),
                network=str(self.network_file),
                request_id=request_id,
                observed_amount_axon=250.0,
                observed_confirmations=3,
                observed_chain_id=8210,
                strict_rpc=True,
            ),
            0,
        )
        self.assertEqual(axonctl.build_scale_plan(str(self.state_file), str(self.network_file), str(self.agents_file), request_id), 0)
        self.assertEqual(axonctl.execute_scale(str(self.state_file), str(self.network_file), str(self.agents_file), request_id, []), 0)
        ssh_mock.side_effect = [
            (True, "", ""),
            (True, "", ""),
            (True, "Docker version 25.0", ""),
            (True, "running", ""),
            (True, "", ""),
            (True, "running", ""),
            (True, "running", ""),
            (True, "running", ""),
        ]
        self.assertEqual(
            axonctl.remote_deploy(
                state_file=str(self.state_file),
                request_id=request_id,
                hosts_file=str(self.hosts_file),
                host_name="test-host",
                network=str(self.network_file),
                agents=str(self.agents_file),
                dry_run=False,
            ),
            0,
        )
        self.assertEqual(
            axonctl.remote_status(
                state_file=str(self.state_file),
                request_id=request_id,
                hosts_file=str(self.hosts_file),
                host_name="test-host",
            ),
            0,
        )

    @mock.patch("axonctl.run_ssh", return_value=(True, "", ""))
    def test_remote_deploy_dry_run(self, _ssh_mock: mock.Mock) -> None:
        self.assertEqual(
            axonctl.remote_deploy(
                state_file=str(self.state_file),
                request_id="dummy",
                hosts_file=str(self.hosts_file),
                host_name="test-host",
                network=str(self.network_file),
                agents=str(self.agents_file),
                dry_run=True,
            ),
            1,
        )

    @mock.patch(
        "axonctl._register_agent_onchain",
        return_value=(
            True,
            {
                "status": "dry_run",
                "registration": {
                    "status": "dry_run",
                    "tx_hash": "",
                    "receipt_status": None,
                    "block_number": None,
                    "from": "0x2222222222222222222222222222222222222222",
                    "to": axonctl.REGISTRY_PRECOMPILE,
                    "value_axon": 100.0,
                    "method": axonctl.REGISTER_METHOD_SIGNATURE,
                    "burn_expected_axon": 20,
                    "evidence_mode": "register_payable_path_proof",
                    "post_check": {"is_agent": False, "agent_id": "", "reputation": 0, "is_online": False},
                },
            },
        ),
    )
    def test_register_onchain_once_dry_run_does_not_write_state(self, _register_mock: mock.Mock) -> None:
        state = axonctl.load_state(str(self.state_file))
        state["agents"]["agent-001"] = {"wallet_address": "0x2222222222222222222222222222222222222222"}
        state["wallets"]["a1"] = {
            "address": "0x2222222222222222222222222222222222222222",
            "private_key": "a" * 64,
            "role": "agent",
            "label": "agent:agent-001",
        }
        axonctl.save_state(str(self.state_file), state)
        before = axonctl.load_state(str(self.state_file))
        self.assertEqual(
            axonctl.register_onchain_once(
                state_file=str(self.state_file),
                network=str(self.network_file),
                agent="agent-001",
                stake_axon=100.0,
                wait_receipt_timeout=180,
                dry_run=True,
                capabilities=axonctl.DEFAULT_REGISTER_CAPABILITIES,
                model=axonctl.DEFAULT_REGISTER_MODEL,
            ),
            0,
        )
        after = axonctl.load_state(str(self.state_file))
        self.assertEqual(before, after)

    @mock.patch(
        "axonctl._register_agent_onchain",
        return_value=(
            True,
            {
                "status": "registered_onchain",
                "registration": {
                    "status": "registered_onchain",
                    "tx_hash": "0xbbb",
                    "receipt_status": 1,
                    "block_number": 1888,
                    "from": "0x2222222222222222222222222222222222222222",
                    "to": axonctl.REGISTRY_PRECOMPILE,
                    "value_axon": 100.0,
                    "method": axonctl.REGISTER_METHOD_SIGNATURE,
                    "burn_expected_axon": 20,
                    "evidence_mode": "register_payable_path_proof",
                    "post_check": {"is_agent": True, "agent_id": "agent-proof", "reputation": 10, "is_online": True},
                },
            },
        ),
    )
    def test_register_onchain_once_updates_state(self, _register_mock: mock.Mock) -> None:
        state = axonctl.load_state(str(self.state_file))
        state["agents"]["agent-001"] = {"wallet_address": "0x2222222222222222222222222222222222222222"}
        state["wallets"]["a1"] = {
            "address": "0x2222222222222222222222222222222222222222",
            "private_key": "a" * 64,
            "role": "agent",
            "label": "agent:agent-001",
        }
        axonctl.save_state(str(self.state_file), state)
        self.assertEqual(
            axonctl.register_onchain_once(
                state_file=str(self.state_file),
                network=str(self.network_file),
                agent="agent-001",
                stake_axon=100.0,
                wait_receipt_timeout=180,
                dry_run=False,
                capabilities=axonctl.DEFAULT_REGISTER_CAPABILITIES,
                model=axonctl.DEFAULT_REGISTER_MODEL,
            ),
            0,
        )
        after = axonctl.load_state(str(self.state_file))
        self.assertTrue(after["agents"]["agent-001"]["registered"])
        self.assertTrue(after["agents"]["agent-001"]["staked"])
        self.assertEqual(after["agents"]["agent-001"]["registration"]["tx_hash"], "0xbbb")

    @mock.patch("axonctl._register_agent_onchain")
    def test_register_onchain_batch_updates_request_failed_agents(self, register_mock: mock.Mock) -> None:
        register_mock.side_effect = [
            (
                True,
                {
                    "status": "registered_onchain",
                    "registration": {
                        "status": "registered_onchain",
                        "tx_hash": "0x111",
                        "receipt_status": 1,
                        "block_number": 10,
                        "from": "0x2222222222222222222222222222222222222222",
                        "to": axonctl.REGISTRY_PRECOMPILE,
                        "value_axon": 100.0,
                        "method": axonctl.REGISTER_METHOD_SIGNATURE,
                        "burn_expected_axon": 20,
                        "evidence_mode": "register_payable_path_proof",
                        "post_check": {"is_agent": True, "agent_id": "agent-a", "reputation": 10, "is_online": True},
                    },
                },
            ),
            (
                False,
                {
                    "error": "insufficient funds",
                    "status": "failed",
                    "registration": {
                        "status": "failed",
                        "tx_hash": "",
                        "receipt_status": None,
                        "block_number": None,
                        "from": "0x3333333333333333333333333333333333333333",
                        "to": axonctl.REGISTRY_PRECOMPILE,
                        "value_axon": 100.0,
                        "method": axonctl.REGISTER_METHOD_SIGNATURE,
                        "burn_expected_axon": 20,
                        "evidence_mode": "register_payable_path_proof",
                        "post_check": {"is_agent": False, "agent_id": "", "reputation": 0, "is_online": False},
                    },
                },
            ),
        ]
        state = axonctl.load_state(str(self.state_file))
        state["requests"]["r1"] = {
            "request_id": "r1",
            "status": "PLANNED",
            "scale_plan": {"agents": ["agent-001", "agent-002"]},
            "execution": {"completed_agents": [], "failed_agents": {}, "attempts": {}},
            "updated_at": 0,
        }
        state["agents"]["agent-001"] = {"wallet_address": "0x2222222222222222222222222222222222222222"}
        state["agents"]["agent-002"] = {"wallet_address": "0x3333333333333333333333333333333333333333"}
        state["wallets"]["a1"] = {
            "address": "0x2222222222222222222222222222222222222222",
            "private_key": "a" * 64,
            "role": "agent",
            "label": "agent:agent-001",
        }
        state["wallets"]["a2"] = {
            "address": "0x3333333333333333333333333333333333333333",
            "private_key": "b" * 64,
            "role": "agent",
            "label": "agent:agent-002",
        }
        axonctl.save_state(str(self.state_file), state)
        self.assertEqual(
            axonctl.register_onchain_batch(
                state_file=str(self.state_file),
                network=str(self.network_file),
                request_id="r1",
                stake_axon=100.0,
                wait_receipt_timeout=180,
                dry_run=False,
                capabilities=axonctl.DEFAULT_REGISTER_CAPABILITIES,
                model=axonctl.DEFAULT_REGISTER_MODEL,
            ),
            1,
        )
        after = axonctl.load_state(str(self.state_file))
        req = after["requests"]["r1"]["execution"]
        self.assertIn("agent-001", req["completed_agents"])
        self.assertIn("agent-002", req["failed_agents"])

    @mock.patch("axonctl.rpc_chain_id", return_value=(True, 8210, None))
    @mock.patch(
        "axonctl._register_agent_onchain",
        return_value=(
            False,
            {
                "error": "insufficient funds",
                "status": "failed",
                "registration": {
                    "status": "failed",
                    "tx_hash": "",
                    "receipt_status": None,
                    "block_number": None,
                    "from": "0x2222222222222222222222222222222222222222",
                    "to": axonctl.REGISTRY_PRECOMPILE,
                    "value_axon": 100.0,
                    "method": axonctl.REGISTER_METHOD_SIGNATURE,
                    "burn_expected_axon": 20,
                    "evidence_mode": "register_payable_path_proof",
                    "post_check": {"is_agent": False, "agent_id": "", "reputation": 0, "is_online": False},
                },
            },
        ),
    )
    @mock.patch(
        "axonctl._ensure_agent_wallet",
        return_value={
            "key_id": "testkey",
            "address": "0x2222222222222222222222222222222222222222",
            "private_key": "0x" + "a" * 64,
        },
    )
    def test_execute_scale_register_failure_does_not_fake_registered(
        self, _wallet_mock: mock.Mock, _register_mock: mock.Mock, _rpc_mock: mock.Mock
    ) -> None:
        self.assertEqual(
            axonctl.create_request(
                state_file=str(self.state_file),
                target_agents=1,
                min_funding_axon=150.0,
                funding_address=self.valid_address,
                min_confirmations=2,
                timeout_sec=600,
                stake_per_agent_axon=100.0,
            ),
            0,
        )
        request_id = next(iter(axonctl.load_state(str(self.state_file))["requests"]))
        self.assertEqual(
            axonctl.fund_check(
                state_file=str(self.state_file),
                network=str(self.network_file),
                request_id=request_id,
                observed_amount_axon=150.0,
                observed_confirmations=3,
                observed_chain_id=8210,
                strict_rpc=True,
            ),
            0,
        )
        self.assertEqual(axonctl.build_scale_plan(str(self.state_file), str(self.network_file), str(self.agents_file), request_id), 0)
        self.assertEqual(axonctl.execute_scale(str(self.state_file), str(self.network_file), str(self.agents_file), request_id, []), 0)
        after = axonctl.load_state(str(self.state_file))
        self.assertIn("agent-001", after["requests"][request_id]["execution"]["failed_agents"])
        self.assertFalse(after["agents"]["agent-001"]["registered"])

    def test_wallet_generate_and_list_and_export(self) -> None:
        self.assertEqual(axonctl.wallet_generate(str(self.state_file), role="funding", label="test-funding"), 0)
        self.assertEqual(axonctl.wallet_generate(str(self.state_file), role="funding", label="test-funding-2"), 0)
        self.assertEqual(axonctl.wallet_list(str(self.state_file)), 0)
        key_id = next(iter(axonctl.load_state(str(self.state_file))["wallets"]))
        self.assertEqual(axonctl.wallet_export(str(self.state_file), key_id, reveal_secret=False), 0)
        backup_file = Path(self.temp_dir.name) / "wallet_backup.json"
        self.assertEqual(axonctl.wallet_backup_export(str(self.state_file), str(backup_file)), 0)
        self.assertEqual(axonctl.wallet_backup_verify(str(backup_file)), 0)

    @mock.patch("axonctl.rpc_chain_id", return_value=(True, 8210, None))
    def test_validate_fails_with_invalid_heartbeat_settings(self, _rpc_mock: mock.Mock) -> None:
        self.network_file.write_text(
            yaml.safe_dump(
                {
                    "rpc_url": "https://mainnet-rpc.axonchain.ai/",
                    "evm_chain_id": 8210,
                    "cosmos_chain_id": "axon_8210-1",
                    "heartbeat": {"interval_blocks": 200, "timeout_blocks": 100},
                },
                sort_keys=False,
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        self.assertEqual(axonctl.validate(str(self.network_file), str(self.agents_file), strict_rpc=True), 1)

    @mock.patch("axonctl._submit_heartbeat_tx", return_value=(True, {"attempts": 1, "tx_hash": "0xabc", "block_height": 123456, "latency_ms": 50}))
    @mock.patch("axonctl.request.urlopen", side_effect=RuntimeError("skip block query"))
    def test_heartbeat_once_updates_state(self, _urlopen_mock: mock.Mock, _submit_mock: mock.Mock) -> None:
        state = axonctl.load_state(str(self.state_file))
        state["agents"]["agent-001"] = {"registered": True, "wallet_address": "0x2222222222222222222222222222222222222222"}
        state["wallets"]["a1"] = {
            "address": "0x2222222222222222222222222222222222222222",
            "private_key": "a" * 64,
            "role": "agent",
            "label": "agent:agent-001",
        }
        axonctl.save_state(str(self.state_file), state)
        self.assertEqual(axonctl.heartbeat_once(str(self.state_file), str(self.network_file), "agent-001", None, None, None), 0)
        after = axonctl.load_state(str(self.state_file))
        self.assertEqual(after["agents"]["agent-001"]["last_heartbeat_tx"], "0xabc")
        self.assertEqual(after["agents"]["agent-001"]["last_heartbeat_block"], 123456)

    @mock.patch("axonctl._submit_heartbeat_tx", return_value=(True, {"attempts": 1, "tx_hash": "0xdef", "block_height": 223456, "latency_ms": 60}))
    @mock.patch("axonctl.request.urlopen", side_effect=RuntimeError("skip block query"))
    def test_heartbeat_batch_with_request_id(self, _urlopen_mock: mock.Mock, _submit_mock: mock.Mock) -> None:
        state = axonctl.load_state(str(self.state_file))
        state["requests"]["r1"] = {"scale_plan": {"agents": ["agent-001", "agent-002"]}}
        state["agents"]["agent-001"] = {"registered": True, "wallet_address": "0x2222222222222222222222222222222222222222"}
        state["agents"]["agent-002"] = {"registered": True, "wallet_address": "0x3333333333333333333333333333333333333333"}
        state["wallets"]["a1"] = {
            "address": "0x2222222222222222222222222222222222222222",
            "private_key": "a" * 64,
            "role": "agent",
            "label": "agent:agent-001",
        }
        state["wallets"]["a2"] = {
            "address": "0x3333333333333333333333333333333333333333",
            "private_key": "b" * 64,
            "role": "agent",
            "label": "agent:agent-002",
        }
        axonctl.save_state(str(self.state_file), state)
        self.assertEqual(axonctl.heartbeat_batch(str(self.state_file), str(self.network_file), "r1", None, None, None), 0)
        after = axonctl.load_state(str(self.state_file))
        self.assertEqual(after["agents"]["agent-001"]["last_heartbeat_tx"], "0xdef")
        self.assertEqual(after["agents"]["agent-002"]["last_heartbeat_tx"], "0xdef")

    @mock.patch("axonctl.get_current_block", return_value=7201)
    def test_challenge_gate_check_rejects_non_validator(self, _block_mock: mock.Mock) -> None:
        state = axonctl.load_state(str(self.state_file))
        state["agents"]["agent-001"] = {"registered": True, "suspended": False, "validator_active": False}
        axonctl.save_state(str(self.state_file), state)
        self.assertEqual(axonctl.challenge_gate_check(str(self.state_file), str(self.network_file), "agent-001"), 1)

    @mock.patch("axonctl.challenge_gate_check", return_value=0)
    @mock.patch("axonctl.get_current_block", return_value=100)
    @mock.patch("axonctl.fetch_challenge_pool")
    @mock.patch("axonctl.load_answer_bank")
    def test_challenge_run_once_non_llm_success(self, bank_mock: mock.Mock, pool_mock: mock.Mock, _block_mock: mock.Mock, _gate_mock: mock.Mock) -> None:
        question = "What is the time complexity of binary search?"
        answer = "O(log n)"
        pool_mock.return_value = [{"question": question, "answer_hash": axonctl.answer_hash(answer), "category": "algorithms"}]
        bank_mock.return_value = {question: answer}
        state = axonctl.load_state(str(self.state_file))
        state["agents"]["agent-001"] = {"registered": True, "validator_active": True}
        axonctl.save_state(str(self.state_file), state)
        self.assertEqual(axonctl.challenge_run_once(str(self.state_file), str(self.network_file), "agent-001"), 0)
        after = axonctl.load_state(str(self.state_file))
        self.assertEqual(after["agents"]["agent-001"]["last_challenge_result"], "success")

    @mock.patch("axonctl.challenge_gate_check", return_value=0)
    @mock.patch("axonctl.get_current_block", return_value=100)
    @mock.patch("axonctl.fetch_challenge_pool")
    @mock.patch("axonctl.load_answer_bank")
    def test_challenge_run_once_hash_mismatch_fails(self, bank_mock: mock.Mock, pool_mock: mock.Mock, _block_mock: mock.Mock, _gate_mock: mock.Mock) -> None:
        question = "What is the time complexity of binary search?"
        pool_mock.return_value = [{"question": question, "answer_hash": axonctl.answer_hash("wrong"), "category": "algorithms"}]
        bank_mock.return_value = {question: "O(log n)"}
        state = axonctl.load_state(str(self.state_file))
        state["agents"]["agent-001"] = {"registered": True, "validator_active": True}
        axonctl.save_state(str(self.state_file), state)
        self.assertEqual(axonctl.challenge_run_once(str(self.state_file), str(self.network_file), "agent-001"), 1)

    @mock.patch("axonctl.get_current_block", return_value=1000)
    def test_lifecycle_report_levels(self, _block_mock: mock.Mock) -> None:
        state = axonctl.load_state(str(self.state_file))
        state["agents"]["agent-001"] = {"registered": True, "staked": True, "service_active": True, "last_heartbeat_block": 990, "last_challenge_result": "success"}
        state["agents"]["agent-002"] = {"registered": True, "staked": True, "service_active": True, "last_heartbeat_block": 100, "last_challenge_result": "failed"}
        axonctl.save_state(str(self.state_file), state)
        self.assertEqual(axonctl.lifecycle_report(str(self.state_file), str(self.network_file), None), 0)

    @mock.patch("axonctl._query_agent_onchain")
    def test_registration_audit_targets_agent_over_request(self, query_mock: mock.Mock) -> None:
        state = axonctl.load_state(str(self.state_file))
        state["requests"]["r1"] = {"scale_plan": {"agents": ["agent-001", "agent-002"]}}
        state["agents"]["agent-001"] = {"wallet_address": "0x1111111111111111111111111111111111111111", "registered": True, "staked": True}
        state["agents"]["agent-002"] = {"wallet_address": "0x2222222222222222222222222222222222222222", "registered": True, "staked": True}
        state["agents"]["agent-003"] = {
            "wallet_address": "0x3333333333333333333333333333333333333333",
            "registered": True,
            "staked": True,
            "registration": {"method": axonctl.REGISTER_METHOD_SIGNATURE, "to": axonctl.REGISTRY_PRECOMPILE, "receipt_status": 1},
        }
        axonctl.save_state(str(self.state_file), state)
        query_mock.return_value = (
            True,
            {
                "is_agent": True,
                "agent_id": "agent-x",
                "reputation": 5,
                "is_online": True,
                "burned_at_register": {"denom": "aaxon", "amount": "20000000000000000000"},
            },
        )
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(
                axonctl.registration_audit(
                    state_file=str(self.state_file),
                    network=str(self.network_file),
                    request_id="r1",
                    agent_names=["agent-003"],
                    strict=False,
                ),
                0,
            )
            payload = json.loads(out.getvalue())
        self.assertEqual(payload["target_count"], 1)
        self.assertEqual(payload["items"][0]["agent"], "agent-003")
        self.assertEqual(query_mock.call_count, 1)

    @mock.patch("axonctl._query_agent_onchain")
    def test_registration_audit_strict_fails_for_unregistered_and_query_error(self, query_mock: mock.Mock) -> None:
        state = axonctl.load_state(str(self.state_file))
        state["agents"]["agent-001"] = {"wallet_address": "0x1111111111111111111111111111111111111111", "registered": False, "staked": False}
        state["agents"]["agent-002"] = {"wallet_address": "0x2222222222222222222222222222222222222222", "registered": True, "staked": True}
        axonctl.save_state(str(self.state_file), state)
        query_mock.side_effect = [
            (True, {"is_agent": False, "agent_id": "", "reputation": 0, "is_online": False, "burned_at_register": {}}),
            (False, {"error": "rpc not connected"}),
        ]
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(
                axonctl.registration_audit(
                    state_file=str(self.state_file),
                    network=str(self.network_file),
                    request_id=None,
                    agent_names=["agent-001", "agent-002"],
                    strict=True,
                ),
                1,
            )
            payload = json.loads(out.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["summary"]["query_failed_count"], 1)
        self.assertEqual(payload["summary"]["unregistered_onchain_count"], 1)
        self.assertEqual(payload["items"][0]["classification"], "unregistered_onchain")
        self.assertIn("query_error", payload["items"][1])

    @mock.patch("axonctl._query_agent_onchain")
    @mock.patch("axonctl.get_current_block", return_value=1000)
    def test_lifecycle_report_includes_registration_evidence_fields(self, _block_mock: mock.Mock, query_mock: mock.Mock) -> None:
        state = axonctl.load_state(str(self.state_file))
        state["agents"]["agent-001"] = {
            "wallet_address": "0x1111111111111111111111111111111111111111",
            "registered": True,
            "staked": True,
            "service_active": True,
            "last_heartbeat_block": 995,
            "last_challenge_result": "success",
            "registration": {"method": axonctl.REGISTER_METHOD_SIGNATURE, "to": axonctl.REGISTRY_PRECOMPILE, "receipt_status": 1},
        }
        state["agents"]["agent-002"] = {
            "wallet_address": "0x2222222222222222222222222222222222222222",
            "registered": True,
            "staked": True,
            "service_active": True,
            "last_heartbeat_block": 995,
            "last_challenge_result": "success",
            "registration": {},
        }
        axonctl.save_state(str(self.state_file), state)
        query_mock.side_effect = [
            (
                True,
                {
                    "is_agent": True,
                    "agent_id": "agent-a",
                    "reputation": 4,
                    "is_online": True,
                    "burned_at_register": {"denom": "aaxon", "amount": "20000000000000000000"},
                },
            ),
            (True, {"is_agent": True, "agent_id": "agent-b", "reputation": 4, "is_online": False, "burned_at_register": {}}),
        ]
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(axonctl.lifecycle_report(str(self.state_file), str(self.network_file), None), 0)
            payload = json.loads(out.getvalue())
        self.assertIn("registration_path_counts", payload["summary"])
        self.assertIn("burn_evidence_counts", payload["summary"])
        self.assertEqual(payload["summary"]["registration_path_counts"]["precompile_register_payable"], 1)
        self.assertEqual(payload["summary"]["registration_path_counts"]["legacy_or_unknown"], 1)
        self.assertEqual(payload["summary"]["burn_evidence_counts"]["onchain_burn_field"], 1)
        self.assertEqual(payload["summary"]["burn_evidence_counts"]["none"], 1)
        self.assertIn("registration_path", payload["items"][0])
        self.assertIn("burn_evidence_level", payload["items"][0])

    @mock.patch("axonctl.challenge_run_once", return_value=0)
    @mock.patch("axonctl.heartbeat_once", return_value=0)
    @mock.patch("axonctl.get_current_block", return_value=1000)
    def test_lifecycle_repair_runs_actions(self, _block_mock: mock.Mock, _hb_mock: mock.Mock, _ch_mock: mock.Mock) -> None:
        state = axonctl.load_state(str(self.state_file))
        state["agents"]["agent-001"] = {"registered": True, "staked": True, "service_active": False, "last_heartbeat_block": 100, "last_challenge_result": "failed"}
        axonctl.save_state(str(self.state_file), state)
        self.assertEqual(axonctl.lifecycle_repair(str(self.state_file), str(self.network_file), None), 0)

    # ── Challenge answer bank tests ─────────────────────────────────────────

    def test_load_answer_bank_fills_missing(self) -> None:
        bank = axonctl.load_answer_bank(str(Path(__file__).resolve().parents[1] / "configs" / "challenge_answers.yaml"))
        filled = sum(1 for v in bank.values() if v)
        self.assertEqual(len(bank), 110)
        self.assertGreaterEqual(filled, 88)

    def test_challenge_validate_simulate_mode(self) -> None:
        # validate_challenge_settings expects the challenge sub-dict (not wrapped in {"challenge": ...})
        cfg = {
            "enabled": True,
            "execution_mode": "simulate",
            "bank_source_url": "http://x",
            "ai_challenge_window_blocks": 50,
        }
        errs = axonctl.validate_challenge_settings(cfg)
        self.assertEqual(errs, [])

    def test_challenge_validate_command_mode(self) -> None:
        cfg = {
            "enabled": True,
            "execution_mode": "command",
            "bank_source_url": "http://x",
            "ai_challenge_window_blocks": 50,
            "command": {"submit_template": "axond tx agent submit {key}", "reveal_template": "axond tx agent reveal {key}"},
        }
        errs = axonctl.validate_challenge_settings(cfg)
        self.assertEqual(errs, [])

    def test_challenge_validate_invalid_mode(self) -> None:
        cfg = {
            "enabled": True,
            "execution_mode": "badmode",
            "bank_source_url": "http://x",
            "ai_challenge_window_blocks": 50,
        }
        errs = axonctl.validate_challenge_settings(cfg)
        self.assertTrue(any("execution_mode" in e for e in errs))

    def test_challenge_validate_window_blocks_required(self) -> None:
        cfg = {
            "enabled": True,
            "execution_mode": "simulate",
            "bank_source_url": "http://x",
        }
        errs = axonctl.validate_challenge_settings(cfg)
        self.assertTrue(any("ai_challenge_window_blocks" in e for e in errs))


if __name__ == "__main__":
    unittest.main()
