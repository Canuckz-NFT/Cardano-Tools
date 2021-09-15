from collections import namedtuple
from datetime import datetime
from pathlib import Path
import subprocess
import requests
import shlex
import json
import sys
import os


class ShelleyError(Exception):
    pass


class ShelleyTools:
    def __init__(
        self,
        path_to_cli,
        path_to_socket,
        working_dir,
        ttl_buffer=1000,
        ssh=None,
        network="--mainnet",
        era="--mary-era",
    ):

        # Debug flag -- may be set after object initialization.
        self.debug = False

        # If the host is remote a Connection object (fabric) is supplied.
        # Set this first because its used during setup.
        self.ssh = ssh

        # Set the socket path, it must be set as an environment variable.
        # Set this first because its used during setup.
        self.socket = path_to_socket

        # Set the path to the CLI and verify it works. An exception will be
        # thrown if the command is not found.
        self.cli = path_to_cli
        self.run_cli(f"{self.cli} --version")

        # Set the working directory and make sure it exists.
        self.working_dir = Path(working_dir)
        if self.ssh is None:
            self.working_dir.mkdir(parents=True, exist_ok=True)
        else:
            self.run_cli(f'mkdir -p "{self.working_dir}"')

        self.ttl_buffer = ttl_buffer
        self.network = network
        self.era = era
        self.protocol_parameters = None

    def run_cli(self, cmd):
        if self.ssh is not None:

            self.ssh.open()  # Open the connection

            # Run the commands remotely
            cmd = f"export CARDANO_NODE_SOCKET_PATH={self.socket}; " + cmd
            if self.debug:
                print(f'CMD: "{cmd}"')
                result = self.ssh.run(cmd, warn=True)
                print(f'stdout: "{result.stdout}"')
                print(f'stderr: "{result.stderr}"')
            else:
                result = self.ssh.run(cmd, warn=True, hide=True)
            stdout = result.stdout.strip()
            stderr = result.stderr.strip()

            self.ssh.close()  # Close the connection

        else:

            # Execute the commands locally
            os.environ["CARDANO_NODE_SOCKET_PATH"] = self.socket
            result = subprocess.run(shlex.split(cmd), capture_output=True)
            stdout = result.stdout.decode().strip()
            stderr = result.stderr.decode().strip()
            if self.debug:
                print(f'CMD: "{cmd}"')
                print(f'stdout: "{stdout}"')
                print(f'stderr: "{stderr}"')

        ResultType = namedtuple("Result", "stdout, stderr")
        return ResultType(stdout, stderr)

    def _load_text_file(self, fpath):
        if self.ssh is not None:
            # Open the connection
            self.ssh.open()

            # Run the commands remotely
            result = self.ssh.run(f"cat {fpath}", warn=True, hide=True)
            text = result.stdout

            # Close the connection
            self.ssh.close()

        else:
            text = open(fpath, "r").read()

        return text

    def _dump_text_file(self, fpath, datastr):
        if self.ssh is not None:

            # Run the commands remotely
            self.ssh.open()  # Open the connection
            cmd = f'printf "%s" \'{datastr}\' > "{fpath}"'
            self.ssh.run(cmd, warn=True, hide=True)
            self.ssh.close()  # Close the connection

        else:
            with open(fpath, "w") as outfile:
                outfile.write(datastr)

    def _download_file(self, url, fpath):
        if self.ssh is not None:

            # Run the commands remotely
            self.ssh.open()  # Open the connection
            cmd = f"curl -sSL {url} -o {fpath}"
            self.ssh.run(cmd, warn=True, hide=True)
            self.ssh.close()  # Close the connection

        else:
            download = requests.get(url)
            with open(fpath, "wb") as download_file:
                download_file.write(download.content)

    def _cleanup_file(self, fpath):
        if self.ssh is not None:

            # Run the commands remotely
            self.ssh.open()  # Open the connection
            self.ssh.run(f"rm {fpath}", warn=True, hide=True)
            self.ssh.close()  # Close the connection

        else:
            os.remove(fpath)

    def load_protocol_parameters(self):
        """Load the protocol parameters which are needed for creating
        transactions.
        """
        params_file = self.working_dir / "protocol.json"
        self.run_cli(
            f"{self.cli} query protocol-parameters {self.network} "
            f"--out-file {params_file}"
        )
        json_data = self._load_text_file(params_file)
        self.protocol_parameters = json.loads(json_data)
        return params_file

    def get_tip(self) -> int:
        """Query the node for the current tip of the blockchain.
        """
        cmd = f"{self.cli} query tip {self.network}"
        result = self.run_cli(cmd)
        if "slot" not in result.stdout:
            raise ShelleyError(result.stderr)
        vals = json.loads(result.stdout)
        return vals["slot"]

    def make_address(self, name, folder=None) -> str:
        """Create an address and the corresponding payment and staking keys.
        """
        if folder is None:
            folder = self.working_dir

        folder = Path(folder)
        if self.ssh is None:
            folder.mkdir(parents=True, exist_ok=True)
        else:
            self.run_cli(f'mkdir -p "{folder}"')
        payment_vkey = folder / (name + ".vkey")
        payment_skey = folder / (name + ".skey")
        stake_vkey = folder / (name + "_stake.vkey")
        stake_skey = folder / (name + "_stake.skey")
        payment_addr = folder / (name + ".addr")
        stake_addr = folder / (name + "_stake.addr")

        # Generate payment key pair.
        self.run_cli(
            f"{self.cli} address key-gen "
            f"--verification-key-file {payment_vkey} "
            f"--signing-key-file {payment_skey}"
        )

        # Generate stake key pair.
        self.run_cli(
            f"{self.cli} stake-address key-gen "
            f"--verification-key-file {stake_vkey} "
            f"--signing-key-file {stake_skey}"
        )

        # Create the payment address.
        self.run_cli(
            f"{self.cli} address build "
            f"--payment-verification-key-file {payment_vkey} "
            f"--stake-verification-key-file {stake_vkey} "
            f"--out-file {payment_addr} {self.network}"
        )

        # Create the staking address.
        self.run_cli(
            f"{self.cli} stake-address build "
            f"--stake-verification-key-file {stake_vkey} "
            f"--out-file {stake_addr} {self.network}"
        )

        # Read the file and return the payment address.
        addr = self._load_text_file(payment_addr).strip()
        return addr

    def get_key_hash(self, vkey_path) -> str:
        """Generate a public key hash from a verification key file.

        Parameters
        ----------
        vkey_path : str, Path
            Path to the verification key file.

        Returns
        -------
        str
            The key hash.
        """
        result = self.run_cli(
            f"{self.cli} address key-hash "
            f"--payment-verification-key-file {vkey_path}"
        )
        return result.stdout

    def get_utxos(self, addr, filter=None) -> list:
        """Query the list of UTXOs for a given address and parse the output.
        The returned data is formatted as a list of dict objects.

        Parameters
        ----------
        addr : str
            Address for which to find the UTXOs.
        filter : str, optional
            Filter the UTXOs based on a token ID. If "Lovelace" is passed to
            the filter, UTXOs containing ONLY lovelace will be returned.
 
        Returns
        -------
        list
            List of UTXOs parsed into dictionary objects.
        """

        # Query the UTXOs for the given address (this will not get everything
        # for a given wallet that contains multiple addresses.)
        result = self.run_cli(
            f"{self.cli} query utxo --address {addr} {self.network}"
        )
        raw_utxos = result.stdout.split("\n")[2:]

        # Parse the UTXOs into a list of dict objects
        utxos = []
        for utxo_line in raw_utxos:
            vals = utxo_line.split()
            utxo_dict = {
                "TxHash": vals[0],
                "TxIx": vals[1],
                "Lovelace": vals[2],
            }

            # Extra tokens will be separated by a "+" sign.
            extra = [i for i, j in enumerate(vals) if j == "+"]
            for i in extra:
                try:
                    asset = vals[i + 2]
                    amt = vals[i + 1]
                    if asset in utxo_dict:
                        utxo_dict[asset] += amt
                    else:
                        utxo_dict[asset] = amt
                except:
                    print("skipping alonzo extra datum")
                
            utxos.append(utxo_dict)

        # Filter utxos
        if filter is not None:
            if filter == "Lovelace":
                utxos = [
                    utxo
                    for utxo in utxos
                    if filter in utxo and len(utxo.keys()) == 3
                ]
            else:
                utxos = [utxo for utxo in utxos if filter in utxo]

        return utxos

    def query_balance(self, addr) -> int:
        """Query an address balance in lovelace.
        """
        total = 0
        utxos = self.get_utxos(addr)
        for utxo in utxos:
            total += int(utxo["Lovelace"])
        return total

    def calc_min_fee(
        self,
        tx_draft,
        tx_in_count,
        tx_out_count,
        witness_count,
        byron_witness_count=0,
    ) -> int:
        """Calculate the minimum fee in lovelaces for the transaction.

        Parameters
        ----------
        tx_draft : str, Path
            Path to draft transaction file.
        tx_in_count : int
            The number of UTXOs being spent.
        tx_out_count : int
            The number of output UTXOs.
        witness_count : int
            The number of transaction signing keys.
        byron_witness_count : int, optional
            Number of Byron witnesses (defaults to 0).
        
        Returns
        -------
        int
            The minimum fee in lovelaces.
        """
        params_file = self.load_protocol_parameters()
        result = self.run_cli(
            f"{self.cli} transaction calculate-min-fee "
            f"--tx-body-file {tx_draft} "
            f"--tx-in-count {tx_in_count} "
            f"--tx-out-count {tx_out_count} "
            f"--witness-count {witness_count} "
            f"--byron-witness-count {byron_witness_count} "
            f"{self.network} --protocol-params-file {params_file}"
        )
        min_fee = int(result.stdout.split()[0])
        return min_fee

    def send_payment(
        self, amt, to_addr, from_addr, key_file, offline=False, cleanup=True
    ):
        """Send ADA from one address to another.

        Parameters
        ----------
        amt : float
            Amount of ADA to send (before fee).
        to_addr : str
            Address to send the ADA to.
        from_addr : str
            Address to send the ADA from.
        key_file : str or Path
            Path to the send address signing key file.
        offline: bool, optional
            Flag to indicate if the transactions is being generated offline.
            If true (defaults to false), the transaction file is signed but
            not sent.
        cleanup : bool, optional
            Flag that indicates if the temporary transaction files should be
            removed when finished (defaults to True).
        """
        payment = amt * 1_000_000  # ADA to Lovelaces

        # Build the transaction
        tx_raw_file = self.build_raw_transaction(
            from_addr,
            witness_count=1,
            receive_addrs=[to_addr],
            payments=[payment],
            certs=None,
            deposits=0,
            folder=None,
            cleanup=cleanup,
        )

        # Sign the transaction with the signing key
        tx_signed_file = self.sign_transaction(tx_raw_file, [key_file])

        # Delete the intermediate transaction files if specified.
        if cleanup:
            self._cleanup_file(tx_raw_file)

        # Submit the transaction
        if not offline:
            self.submit_transaction(tx_signed_file, cleanup)
        else:
            print(f"Signed transaction file saved to: {tx_signed_file}")

    def register_stake_address(
        self,
        addr,
        stake_vkey_file,
        stake_skey_file,
        pmt_skey_file,
        offline=False,
        cleanup=True,
    ):
        """Register a stake address in the blockchain.

        Parameters
        ----------
        addr : str
            Address of the staking key being registered.
        stake_vkey_file : str or Path
            Path to the staking verification key.
        stake_skey_file : str or Path
            Path to the staking signing key.
        pmt_skey_file : str or Path
            Path to the payment signing key.
        offline: bool, optional
            Flag to indicate if the transactions is being generated offline.
            If true (defaults to false), the transaction file is signed but
            not sent.
        cleanup : bool, optional
            Flag that indicates if the temporary transaction files should be
            removed when finished (defaults to True).
        """

        # Build a transaction name
        tx_name = datetime.now().strftime("reg_stake_key_%Y-%m-%d_%Hh%Mm%Ss")

        # Create a registration certificate
        key_file_path = Path(stake_vkey_file)
        stake_cert_path = key_file_path.parent / (key_file_path.stem + ".cert")
        self.run_cli(
            f"{self.cli} stake-address registration-certificate "
            f"--stake-verification-key-file {stake_vkey_file} "
            f"--out-file {stake_cert_path}"
        )

        # Determine the TTL
        tip = self.get_tip()
        ttl = tip + self.ttl_buffer

        # Get a list of UTXOs and sort them in decending order by value.
        utxos = self.get_utxos(addr)
        if len(utxos) < 1:
            raise ShelleyError(
                f"Transaction failed due to insufficient funds. "
                f"Account {addr} cannot pay tranction costs because "
                "it does not contain any ADA."
            )
        utxos.sort(key=lambda k: k["Lovelace"], reverse=True)

        # Ensure the parameters file exists
        self.load_protocol_parameters()

        # Iterate through the UTXOs until we have enough funds to cover the
        # transaction. Also, create the tx_in string for the transaction.
        tx_draft_file = Path(self.working_dir) / (tx_name + ".draft")
        utxo_total = 0
        tx_in_str = ""
        for idx, utxo in enumerate(utxos):
            utxo_count = idx + 1
            utxo_total += int(utxo["Lovelace"])
            tx_in_str += f" --tx-in {utxo['TxHash']}#{utxo['TxIx']}"

            # Build a transaction draft
            self.run_cli(
                f"{self.cli} transaction build-raw{tx_in_str} "
                f"--tx-out {addr}+0 --ttl 0 --fee 0 "
                f"--certificate-file {stake_cert_path} "
                f"--out-file {tx_draft_file}"
            )

            # Calculate the minimum fee
            min_fee = self.calc_min_fee(
                tx_draft_file, utxo_count, tx_out_count=1, witness_count=2
            )

            # TX cost
            cost = min_fee + self.protocol_parameters["stakeAddressDeposit"]
            if utxo_total > cost:
                break

        if utxo_total < cost:
            cost_ada = cost / 1_000_000
            utxo_total_ada = utxo_total / 1_000_000
            raise ShelleyError(
                f"Transaction failed due to insufficient funds. "
                f"Account {addr} cannot pay tranction costs of {cost_ada} "
                f"ADA because it only contains {utxo_total_ada} ADA."
            )

        # Build the transaction.
        tx_raw_file = Path(self.working_dir) / (tx_name + ".raw")
        self.run_cli(
            f"{self.cli} transaction build-raw{tx_in_str} "
            f"--tx-out {addr}+{utxo_total - cost} "
            f"--ttl {ttl} --fee {min_fee} "
            f"--certificate-file {stake_cert_path} "
            f"--out-file {tx_raw_file}"
        )

        # Sign the transaction with both the payment and stake keys.
        tx_signed_file = Path(self.working_dir) / (tx_name + ".signed")
        self.run_cli(
            f"{self.cli} transaction sign "
            f"--tx-body-file {tx_raw_file} --signing-key-file {pmt_skey_file} "
            f"--signing-key-file {stake_skey_file} {self.network} "
            f"--out-file {tx_signed_file}"
        )

        # Delete the intermediate transaction files if specified.
        if cleanup:
            self._cleanup_file(tx_draft_file)
            self._cleanup_file(tx_raw_file)

        # Submit the transaction
        if not offline:
            self.submit_transaction(tx_signed_file, cleanup)
        else:
            print(f"Signed transaction file saved to: {tx_signed_file}")

    def generate_kes_keys(self, pool_name="pool", folder=None) -> (str, str):
        """Generate a new set of KES keys for a stake pool.

        KES == Key Evolving Signature

        Parameters
        ----------
        pool_name : str
            Pool name for file/certificate naming.
        folder : str or Path, optional
            The directory where the generated files/certs will be placed.
        
        Returns
        _______
        (str, str)
            Paths to the new verification and signing KES key files.
        """

        # Get a working directory to store the generated files and make sure
        # the directory exists.
        if folder is None:
            folder = self.working_dir
        else:
            folder = Path(folder)
            if self.ssh is None:
                folder.mkdir(parents=True, exist_ok=True)
            else:
                self.run_cli(f'mkdir -p "{folder}"')

        # Generate the KES Key pair
        kes_vkey = folder / (pool_name + "_kes.vkey")
        kes_skey = folder / (pool_name + "_kes.skey")
        self.run_cli(
            f"{self.cli} node key-gen-KES "
            f"--verification-key-file {kes_vkey} "
            f"--signing-key-file {kes_skey}"
        )

        return (kes_vkey, kes_skey)

    def create_block_producing_keys(
        self, genesis_file, pool_name="pool", folder=None
    ):
        """Create keys for a block-producing node.
        WARNING: You may want to use your local machine for this process
        (assuming you have cardano-node and cardano-cli on it). Make sure you
        are not online until you have put your cold keys in a secure storage
        and deleted the files from you local machine.

        The block-producing node or pool node needs:
            Cold key pair,
            VRF Key pair,
            KES Key pair,
            Operational Certificate
        
        Parameters
        ----------
        genesis_file : str or Path
            Path to the genesis file.
        pool_name : str
            Pool name for file/certificate naming.
        folder : str or Path, optional
            The directory where the generated files/certs will be placed.
        """

        # Get a working directory to store the generated files and make sure
        # the directory exists.
        if folder is None:
            folder = self.working_dir
        else:
            folder = Path(folder)
            if self.ssh is None:
                folder.mkdir(parents=True, exist_ok=True)
            else:
                self.run_cli(f'mkdir -p "{folder}"')

        # Generate Cold Keys and a Cold_counter
        cold_vkey = folder / (pool_name + "_cold.vkey")
        cold_skey = folder / (pool_name + "_cold.skey")
        cold_counter = folder / (pool_name + "_cold.counter")
        self.run_cli(
            f"{self.cli} node key-gen "
            f"--cold-verification-key-file {cold_vkey} "
            f"--cold-signing-key-file {cold_skey} "
            f"--operational-certificate-issue-counter-file {cold_counter}"
        )

        # Generate VRF Key pair
        vrf_vkey = folder / (pool_name + "_vrf.vkey")
        vrf_skey = folder / (pool_name + "_vrf.skey")
        self.run_cli(
            f"{self.cli} node key-gen-VRF "
            f"--verification-key-file {vrf_vkey} "
            f"--signing-key-file {vrf_skey}"
        )

        # Generate the KES Key pair
        kes_vkey, kes_skey = self.generate_kes_keys(pool_name, folder)

        # Get the network genesis parameters
        json_data = self._load_text_file(genesis_file)
        genesis_parameters = json.loads(json_data)

        # Generate the Operational Certificate/
        cert_file = folder / (pool_name + ".cert")
        slots_kes_period = genesis_parameters["slotsPerKESPeriod"]
        tip = self.get_tip()
        kes_period = tip // slots_kes_period  # Integer division
        self.run_cli(
            f"{self.cli} node issue-op-cert "
            f"--kes-verification-key-file {kes_vkey} "
            f"--cold-signing-key-file {cold_skey} "
            f"--operational-certificate-issue-counter {cold_counter} "
            f"--kes-period {kes_period} --out-file {cert_file}"
        )

        # Get the pool ID and return it.
        result = self.run_cli(
            f"{self.cli} stake-pool id "
            f"--cold-verification-key-file {cold_vkey}"
        )
        pool_id = result.stdout
        self._dump_text_file(folder / (pool_name + ".id"), pool_id)

        return pool_id  # Return the pool id after first saving it to a file.

    def update_kes_keys(
        self,
        genesis_file,
        cold_skey,
        cold_counter,
        pool_name="pool",
        folder=None,
    ):
        """Update KES keys for an existing stake pool.

        Parameters
        ----------
        genesis_file : str or Path
            Path to the genesis file.
        cold_skey : str or Path
            Path to the pool's cold signing key.
        cold_counter : str or Path
            Path to the pool's cold counter file.
        pool_name : str
            Pool name for file/certificate naming.
        folder : str or Path, optional
            The directory where the generated files/certs will be placed.
        """

        # Get a working directory to store the generated files and make sure
        # the directory exists.
        if folder is None:
            folder = self.working_dir
        else:
            folder = Path(folder)
            if self.ssh is None:
                folder.mkdir(parents=True, exist_ok=True)
            else:
                self.run_cli(f'mkdir -p "{folder}"')

        # Generate the new KES key pair
        kes_vkey, kes_skey = self.generate_kes_keys(pool_name, folder)

        # Generate the new pool operation certificate
        # Get the network genesis parameters
        json_data = self._load_text_file(genesis_file)
        genesis_parameters = json.loads(json_data)

        # Generate the Operational Certificate
        cert_file = folder / (pool_name + ".cert")
        slots_kes_period = genesis_parameters["slotsPerKESPeriod"]
        tip = self.get_tip()
        kes_period = tip // slots_kes_period  # Integer division
        result = self.run_cli(
            f"{self.cli} node issue-op-cert "
            f"--kes-verification-key-file {kes_vkey} "
            f"--cold-signing-key-file {cold_skey} "
            f"--operational-certificate-issue-counter {cold_counter} "
            f"--kes-period {kes_period} --out-file {cert_file}"
        )

        if result.stderr:
            raise ShelleyError(f"Unable to rotate KES keys: {result.stderr}")


    def create_metadata_file(self, pool_metadata, folder=None) -> str:
        """ Create a JSON file with the pool metadata and return the file hash.
        """

        # Get a working directory to store the generated files and make sure
        # the directory exists.
        if folder is None:
            folder = self.working_dir
        else:
            folder = Path(folder)
            if self.ssh is None:
                folder.mkdir(parents=True, exist_ok=True)
            else:
                self.run_cli(f'mkdir -p "{folder}"')

        # Create a JSON file with the pool metadata and return the file hash.
        ticker = pool_metadata["ticker"]
        metadata_file_path = folder / f"{ticker}_metadata.json"
        self._dump_text_file(
            metadata_file_path, json.dumps(pool_metadata).strip()
        )
        result = self.run_cli(
            f"{self.cli} stake-pool metadata-hash "
            f"--pool-metadata-file {metadata_file_path}"
        )
        metadata_hash = result.stdout.strip()
        return metadata_hash

    def generate_stake_pool_cert(
        self,
        pool_name,
        pool_pledge,
        pool_cost,
        pool_margin,
        pool_cold_vkey,
        pool_vrf_key,
        pool_reward_vkey,
        owner_stake_vkeys,
        pool_relays=None,
        pool_metadata_url=None,
        pool_metadata_hash=None,
        folder=None,
    ) -> str:
        """Generate a stake pool certificate.

        This function generates a stake pool registration certificate. It can 
        be used without connection to a running node for offline applications.

        Parameters
        ----------
        pool_name : str
            Pool name for file/certificate naming.
        pool_metadata : dict
            Dictionary of stake pool metadata to be converted to json.
        pool_pledge : int
            Pool pledge amount in lovelace.
        pool_cost : int
            Pool cost (fixed fee per epoch) in lovelace.
        pool_margin : float
            Pool margin (variable fee) as a percentage.
        pool_cold_vkey : str or Path
            Path to the pool's cold verification key.
        pool_vrf_key : str or Path
            Path to the pool's verification key.
        pool_reward_vkey : str or Path
            Path to the staking verification key that will receive pool
            rewards.
        owner_stake_vkeys : list
            List of owner stake verification keys (paths) responsible for the
            pledge.
        pool_relays: list, optional,
            List of dictionaries each representing a pool relay. The
            dictionaries have three required keys:
                "port" specifying the relay's port number,
                "host" specifying the host name (IP, DNS, etc.),
                "host-type" specifying the type of data in the "host" key.
        pool_metadata_url : str, optional
            URL to the pool's metadata JSON file.
        pool_metadata_hash : str, optional
            Optionally specify the hash of the metadata JSON file. If this is
            not specified and the pool_metadata_hash is, then the code will
            download the file from the URL and compute the hash.
        folder : str or Path, optional
            The directory where the generated files/certs will be placed.
        
        Returns
        -------
        str
            The path to the stake pool registration certificate file.
        """
        # Get a working directory to store the generated files and make sure
        # the directory exists.
        if folder is None:
            folder = self.working_dir
        else:
            folder = Path(folder)
            if self.ssh is None:
                folder.mkdir(parents=True, exist_ok=True)
            else:
                self.run_cli(f'mkdir -p "{folder}"')

        # Get the hash of the JSON file if the URL is provided and the hash is
        # not specified.
        metadata_args = ""
        if pool_metadata_url is not None:
            if pool_metadata_hash is None:
                metadata_file = folder / "metadata_file_download.json"
                self._download_file(pool_metadata_url, metadata_file)
                result = self.run_cli(
                    f"{self.cli} stake-pool metadata-hash "
                    f"--pool-metadata-file {metadata_file}"
                )
                pool_metadata_hash = result.stdout.strip()

            # Create the arg string for the pool cert.
            metadata_args = (
                f"--metadata-url {pool_metadata_url} "
                f"--metadata-hash {pool_metadata_hash}"
            )

        # Create the relay arg string. Basically, we need a port and host arg
        # but there can be different forms of the host argument. See the
        # caradno-cli documentation. The simpliest way I could figure was to
        # use a list of dictionaries where each dict represents a relay.
        relay_args = ""
        for relay in pool_relays:
            if "ipv4" in relay["host-type"]:
                host_arg = f"--pool-relay-ipv4 {relay['host']}"
            elif "ipv6" in relay["host-type"]:
                host_arg = f"--pool-relay-ipv4 {relay['host']}"
            elif "single" in relay["host-type"]:
                host_arg = f"--single-host-pool-relay {relay['host']}"
            elif "multi" in relay["host-type"]:
                relay_args += f"--multi-host-pool-relay {relay['host']}"
                continue  # No port info for this case
            else:
                continue  # Skip if invalid host type
            port_arg = f"--pool-relay-port {relay['port']}"
            relay_args += f"{host_arg} {port_arg} "

        # Create the argument string for the list of owner verification keys.
        owner_vkey_args = ""
        for key_path in owner_stake_vkeys:
            arg = f"--pool-owner-stake-verification-key-file {key_path} "
            owner_vkey_args += arg

        # Generate Stake pool registration certificate
        ts = datetime.now().strftime("tx_%Y-%m-%d_%Hh%Mm%Ss")
        pool_cert_path = folder / (pool_name + "_registration_" + ts + ".cert")
        self.run_cli(
            f"{self.cli} stake-pool registration-certificate "
            f"--cold-verification-key-file {pool_cold_vkey} "
            f"--vrf-verification-key-file {pool_vrf_key} "
            f"--pool-pledge {pool_pledge} "
            f"--pool-cost {pool_cost} "
            f"--pool-margin {pool_margin/100} "
            f"--pool-reward-account-verification-key-file {pool_reward_vkey} "
            f"{owner_vkey_args} {relay_args} {metadata_args} "
            f"{self.network} --out-file {pool_cert_path}"
        )

        # Return the path to the generated pool cert
        return pool_cert_path

    def generate_delegation_cert(
        self, owner_stake_vkeys, pool_cold_vkey, folder=None
    ):
        """Generate a delegation certificate for pledging.

        Parameters
        ----------
        owner_stake_vkeys : list
            List of owner stake verification keys (paths) responsible for the
            pledge.
        pool_cold_vkey : str or Path
            Path to the pool's cold verification key.
        folder : str or Path, optional
            The directory where the generated files/certs will be placed.
        """

        # Get a working directory to store the generated files and make sure
        # the directory exists.
        if folder is None:
            folder = self.working_dir
        else:
            folder = Path(folder)
            if self.ssh is None:
                folder.mkdir(parents=True, exist_ok=True)
            else:
                self.run_cli(f'mkdir -p "{folder}"')

        # Generate delegation certificate (pledge from each owner)
        ts = datetime.now().strftime("tx_%Y-%m-%d_%Hh%Mm%Ss")
        certs = []
        for key_path in owner_stake_vkeys:
            key_path = Path(key_path)
            cert_path = key_path.parent / (
                key_path.stem + "_delegation_" + ts + ".cert"
            )
            self.run_cli(
                f"{self.cli} stake-address delegation-certificate "
                f"--stake-verification-key-file {key_path} "
                f"--cold-verification-key-file {pool_cold_vkey} "
                f"--out-file {cert_path}"
            )
            certs.append(cert_path)

        # Return a list of certificate files
        return certs

    def build_raw_transaction(
        self,
        payment_addr,
        witness_count=1,
        receive_addrs=None,
        payments=None,
        certs=None,
        deposits=0,
        folder=None,
        cleanup=True,
    ) -> str:
        """Build a raw (unsigned) transaction.

        Requires a running and synced node.

        Parameters
        ----------
        payment_addr : str
            Address to pay the fees, deposites, and payments.
        receive_addrs : list, optional
            Address to receive payment.
        payments: list, optional
            Payments (lovelaces) corresponding to the list of receive addresses.
        certs: list, optional
            List of certificate files to include in the transaction.
        deposits: int, optional
            Deposits
        cleanup : bool, optional
            Flag that indicates if the temporary transaction files should be
            removed when finished (defaults to True).

        Returns
        -------
        str
            Resturns the path to the raw transaction file.
        """

        # Get a working directory to store the generated files and make sure
        # the directory exists.
        if folder is None:
            folder = self.working_dir
        else:
            folder = Path(folder)
            if self.ssh is None:
                folder.mkdir(parents=True, exist_ok=True)
            else:
                self.run_cli(f'mkdir -p "{folder}"')

        # Get a list of certificate arguments
        cert_args = ""
        if certs:
            for cert_path in certs:
                cert_args += f"--certificate-file {cert_path} "

        # Sume the total payments
        total_payments = 0
        if payments:
            total_payments = sum(payments)

        # Get a list of payment args
        pymt_args_zero = ""
        pymt_args = ""
        if receive_addrs:
            for addr, amt in zip(receive_addrs, payments):
                pymt_args_zero += f"--tx-out {addr}+0 "
                pymt_args += f"--tx-out {addr}+{amt:.0f} "

        # Get a list of UTXOs and sort them in decending order by value.
        utxos = self.get_utxos(payment_addr, filter="Lovelace")
        utxos.sort(key=lambda k: k["Lovelace"], reverse=True)

        # Determine the TTL
        tip = self.get_tip()
        ttl = tip + self.ttl_buffer

        # Ensure the parameters file exists
        self.load_protocol_parameters()
        min_utxo = self.protocol_parameters["minUTxOValue"]

        # Iterate through the UTXOs until we have enough funds to cover the
        # transaction. Also, create the tx_in string for the transaction.
        tx_name = datetime.now().strftime("tx_%Y-%m-%d_%Hh%Mm%Ss")
        tx_draft_file = Path(self.working_dir) / (tx_name + ".draft")
        lovelaces_out = sys.maxsize  # must be larger than zero
        utxo_total = 0
        min_fee = 1  # make this start greater than utxo_total
        tx_in_str = ""
        for idx, utxo in enumerate(utxos):
            utxo_count = idx + 1
            utxo_total += int(utxo["Lovelace"])
            tx_in_str += f"--tx-in {utxo['TxHash']}#{utxo['TxIx']} "

            # Build a transaction draft
            self.run_cli(
                f"{self.cli} transaction build-raw {tx_in_str}"
                f"--tx-out {payment_addr}+0 {pymt_args_zero} --ttl 0 --fee 0 "
                f"--out-file {tx_draft_file} {cert_args}"
            )

            # Calculate the minimum fee
            min_fee = self.calc_min_fee(
                tx_draft_file,
                utxo_count,
                tx_out_count=1,
                witness_count=witness_count,
            )

            # If we have enough Lovelaces to cover the transaction can stop
            # iterating through the UTXOs.
            lovelaces_out = min_fee + deposits + total_payments
            utxo_amt = utxo_total - lovelaces_out
            if utxo_total > lovelaces_out and (utxo_amt > min_utxo or utxo_amt == 0):
                break

        # Handle the error case where there is not enough inputs for the output
        cost_ada = lovelaces_out / 1_000_000
        utxo_total_ada = utxo_total / 1_000_000
        if utxo_total < lovelaces_out:
            # This is the case where the sending wallet has no UTXOs to spend.
            # The above for loop didn't run at all which is why the
            # lovelaces_out value is still sys.maxsize.
            if lovelaces_out == sys.maxsize:
                raise ShelleyError(
                    f"Transaction failed due to insufficient funds. Account "
                    f"{payment_addr} is empty."
                )    
            raise ShelleyError(
                f"Transaction failed due to insufficient funds. Account "
                f"{payment_addr} cannot pay tranction costs of {cost_ada} "
                f"ADA because it only contains {utxo_total_ada} ADA."
            )

        # Setup the new UTXO
        utxo_str = ""
        if utxo_amt == 0:
            # The transaction is emptying the account. No UTXO.
            pass
        elif utxo_amt < min_utxo:
            # Verify that the UTXO is larger than the minimum.
            raise ShelleyError(
                f"Transaction failed due to insufficient funds. Account "
                f"{payment_addr} cannot pay tranction costs of {cost_ada} "
                f"ADA because it only contains {utxo_total_ada} ADA "
                f"resulting in an UTxO of {utxo_total_ada - cost_ada} ADA "
                f"which is less than the minimum of {min_utxo / 1_000_000}."
            )
        else:
            utxo_str = f"--tx-out {payment_addr}+{utxo_amt}"

        # Build the transaction to the blockchain.
        tx_raw_file = Path(self.working_dir) / (tx_name + ".raw")
        self.run_cli(
            f"{self.cli} transaction build-raw {tx_in_str} {utxo_str} "
            f"{pymt_args} --ttl {ttl} --fee {min_fee} "
            f"--out-file {tx_raw_file} {cert_args}"
        )

        # Delete the intermediate transaction files if specified.
        if cleanup:
            self._cleanup_file(tx_draft_file)

        # Return the path to the raw transaction file.
        return tx_raw_file

    def build_multisignature_scripts(
        self,
        script_name,
        key_hashes,
        sig_type,
        required=None,
        start_slot=None,
        end_slot=None,
        folder=None,
    ) -> str:
        """Helper function for building multi-signature scripts.

        This script is not required as the multi-signature scripts may be created by hand.

        Parameters
        ----------
        name : str
            Name of the script
        key_hashes : list
            List of key hashes (use get_key_hash)
        sig_type : str
            Signature type (all, any, atLeast)
        required : int, optional
            Number of required signatures (used with type="atLeast")
        start_slot : int, optional
            Lower bound on slots where minting is allowed 
        end_slot : int, optional
            Upper bound on slots where minting is allowed

        Returns
        -------
        str
            Path to the multi-signature script file.
        """

        # Get a working directory to store the generated files and make sure
        # the directory exists.
        if folder is None:
            folder = self.working_dir
        else:
            folder = Path(folder)
            if self.ssh is None:
                folder.mkdir(parents=True, exist_ok=True)
            else:
                self.run_cli(f'mkdir -p "{folder}"')

        # Build the list of signature hashes
        script = {
            "scripts": [{"keyHash": h, "type": "sig"} for h in key_hashes]
        }

        # Determine the type. Default to all
        sig_type = sig_type.lower()
        if sig_type == "any":
            script["type"] = "any"
        elif sig_type == "atleast" and required is not None:
            script["type"] = "atLeast"
            script["required"] = int(required)
            if script["required"] < 1 or script["required"] >= len(key_hashes):
                raise ShelleyError("Invalid number of required signatures.")
        else:
            script["type"] = "all"

        # Add bounds
        if start_slot is not None:
            script["scripts"].append({"slot": start_slot, "type": "after"})
        if end_slot is not None:
            script["scripts"].append({"slot": start_slot, "type": "before"})

        # Write the script file
        file_path = Path(folder) / (script_name + ".json")
        with open(file_path, "w") as outfile:
            json.dump(script, outfile, indent=4)

        return file_path

    def witness_transaction(self, tx_file, witnesses) -> str:
        """Sign a transaction file with witness file(s).

        Parameters
        ----------
        tx_file : str or Path
            Path to the transaction file to be signed.
        witnesses : list
            List of paths (str or Path) to the witness files.

        Returns
        -------
        str
            Path to the signed transaction file.
        """

        # Generate a list of witness args.
        witness_args = ""
        for witness in witnesses:
            witness_args += f"--witness-file {witness} "

        # Sign the transaction with the signing key
        tx_name = Path(tx_file).stem
        tx_signed_file = tx_name + ".signed"
        self.run_cli(
            f"{self.cli} transaction sign-witness "
            f"--tx-body-file {tx_file} {witness_args}"
            f"--out-file {tx_signed_file}"
        )

        # Return the path to the signed file for downstream use.
        return tx_signed_file

    def sign_transaction(self, tx_file, skeys) -> str:
        """Sign a transaction file with a signing key.

        Parameters
        ----------
        tx_file : str or Path
            Path to the transaction file to be signed.
        skeys : list
            List of paths (str or Path) to the signing key files.
        
        Returns
        -------
        str
            Path to the signed transaction file.
        """

        # Generate a list of signing key args.
        signing_key_args = ""
        for key_path in skeys:
            signing_key_args += f"--signing-key-file {key_path} "
        
        # Sign the transaction with the signing key
        tx_name = Path(tx_file).stem
        tx_signed_file = tx_name + ".signed"
        result = self.run_cli(
            f"{self.cli} transaction sign "
            f"--tx-body-file {tx_file} {signing_key_args} "
            f"{self.network} --out-file {tx_signed_file}"
        )

        if result.stderr:
            raise ShelleyError(f"Unable to sign transaction: {result.stderr}")

        # Return the path to the signed file for downstream use.
        return tx_signed_file

    def submit_transaction(self, signed_tx_file, cleanup=False):
        """Submit a transaction to the blockchain. This function is separate to
        enable the submissions of transactions signed by offline keys.
        
        Parameters
        ----------
        signed_tx_file : str or Path
            Path to the signed transaction file ready for submission.
        cleanup : bool, optional
            Flag that indicates if the temporary transaction files should be
            removed when finished (defaults to false).
        """

        # Submit the transaction
        result = self.run_cli(
            f"{self.cli} transaction submit "
            f"--tx-file {signed_tx_file} {self.network}"
        )

        if result.stderr:
            raise ShelleyError(f"Unable to submit transaction: {result.stderr}")

        # Delete the transaction files if specified.
        if cleanup:
            self._cleanup_file(signed_tx_file)

    def register_stake_pool(
        self,
        pool_name,
        pool_pledge,
        pool_cost,
        pool_margin,
        pool_cold_vkey,
        pool_cold_skey,
        pool_vrf_key,
        pool_reward_vkey,
        owner_stake_vkeys,
        owner_stake_skeys,
        payment_addr,
        payment_skey,
        genesis_file,
        pool_relays=None,
        pool_metadata_url=None,
        pool_metadata_hash=None,
        folder=None,
        offline=False,
        cleanup=True,
    ):
        """Register a stake pool on the blockchain.

        Parameters
        ----------
        pool_name : str
            Pool name for file/certificate naming.
        pool_metadata : dict
            Dictionary of stake pool metadata to be converted to json.
        pool_pledge : int
            Pool pledge amount in lovelace.
        pool_cost : int
            Pool cost (fixed fee per epoch) in lovelace.
        pool_margin : float
            Pool margin (variable fee) as a percentage.
        pool_cold_vkey : str or Path
            Path to the pool's cold verification key.
        pool_cold_skey : str or Path
            Path to the pool's cold signing key.
        pool_vrf_key : str or Path
            Path to the pool's verification key.
        pool_reward_vkey : str or Path
            Path to the staking verification key that will receive pool
            rewards.
        owner_stake_vkeys : list
            List of owner stake verification keys (paths) responsible for the
            pledge.
        owner_stake_skeys : list
            List of owner stake signing keys (paths) responsible for the
            pledge.
        payment_addr : str
            Address responsible for paying the pool registration and
            transaction fees.
        payment_skey : str or Path
            Signing key for the address responsible for paying the pool
            registration and transaction fees.
        genesis_file : str or Path
            Path to the genesis file.
        pool_relays: list, optional,
            List of dictionaries each representing a pool relay. The
            dictionaries have three required keys:
                "port" specifying the relay's port number,
                "host" specifying the host name (IP, DNS, etc.),
                "host-type" specifying the type of data in the "host" key.
        pool_metadata_url : str, optional
            URL to the pool's metadata JSON file.
        pool_metadata_hash : str, optional
            Optionally specify the hash of the metadata JSON file. If this is
            not specified and the pool_metadata_hash is, then the code will
            download the file from the URL and compute the hash.
        folder : str or Path, optional
            The directory where the generated files/certs will be placed.
        offline: bool, optional
            Flag to indicate if the transactions is being generated offline.
            If true (defaults to false), the transaction file is signed but
            not sent.
        cleanup : bool, optional
            Flag that indicates if the temporary transaction files should be
            removed when finished (defaults to True).
        """

        # Get a working directory to store the generated files and make sure
        # the directory exists.
        if folder is None:
            folder = self.working_dir
        else:
            folder = Path(folder)
            if self.ssh is None:
                folder.mkdir(parents=True, exist_ok=True)
            else:
                self.run_cli(f'mkdir -p "{folder}"')

        pool_cert_path = self.generate_stake_pool_cert(
            pool_name,
            pool_pledge,
            pool_cost,
            pool_margin,
            pool_cold_vkey,
            pool_vrf_key,
            pool_reward_vkey,
            owner_stake_vkeys,
            pool_relays=pool_relays,
            pool_metadata_url=pool_metadata_url,
            pool_metadata_hash=pool_metadata_hash,
            folder=folder,
        )

        # Generate delegation certificates (pledge from each owner)
        del_certs = self.generate_delegation_cert(
            owner_stake_vkeys, pool_cold_vkey, folder=folder
        )
        del_cert_args = ""
        for cert_path in del_certs:
            del_cert_args += f"--certificate-file {cert_path} "

        # Generate a list of owner signing key args.
        signing_key_args = ""
        for key_path in owner_stake_skeys:
            signing_key_args += f"--signing-key-file {key_path} "

        # Get the pool deposit from the network genesis parameters.
        json_data = self._load_text_file(genesis_file)
        pool_deposit = json.loads(json_data)["protocolParams"]["poolDeposit"]

        # Get a list of UTXOs and sort them in decending order by value.
        utxos = self.get_utxos(payment_addr)
        utxos.sort(key=lambda k: k["Lovelace"], reverse=True)

        # Determine the TTL
        tip = self.get_tip()
        ttl = tip + self.ttl_buffer

        # Ensure the parameters file exists
        self.load_protocol_parameters()

        # Iterate through the UTXOs until we have enough funds to cover the
        # transaction. Also, create the tx_in string for the transaction.
        tx_name = datetime.now().strftime("reg_pool_%Y-%m-%d_%Hh%Mm%Ss")
        tx_draft_file = Path(self.working_dir) / (tx_name + ".draft")
        utxo_total = 0
        min_fee = 1  # make this start greater than utxo_total
        tx_in_str = ""
        for idx, utxo in enumerate(utxos):
            utxo_count = idx + 1
            utxo_total += int(utxo["Lovelace"])
            tx_in_str += f" --tx-in {utxo['TxHash']}#{utxo['TxIx']}"

            # Build a transaction draft
            self.run_cli(
                f"{self.cli} transaction build-raw{tx_in_str} "
                f"--tx-out {payment_addr}+0 --ttl 0 --fee 0 "
                f"--out-file {tx_draft_file} "
                f"--certificate-file {pool_cert_path} {del_cert_args}"
            )

            # Calculate the minimum fee
            nwit = len(owner_stake_skeys) + 2
            min_fee = self.calc_min_fee(
                tx_draft_file, utxo_count, tx_out_count=1, witness_count=nwit
            )

            if utxo_total > (min_fee + pool_deposit + 10):
                break

        if utxo_total < (min_fee + pool_deposit):
            cost_ada = (min_fee + pool_deposit) / 1_000_000
            utxo_total_ada = utxo_total / 1_000_000
            raise ShelleyError(
                f"Transaction failed due to insufficient funds. Account "
                f"{payment_addr} cannot pay tranction costs of {cost_ada} "
                f"lovelaces because it only contains {utxo_total_ada} ADA."
            )

        # Build the transaction to submit the pool certificate and delegation
        # certificate(s) to the blockchain.
        tx_raw_file = Path(self.working_dir) / (tx_name + ".raw")
        self.run_cli(
            f"{self.cli} transaction build-raw{tx_in_str} "
            f"--tx-out {payment_addr}+{utxo_total - min_fee - pool_deposit} "
            f"--ttl {ttl} --fee {min_fee} --out-file {tx_raw_file} "
            f"--certificate-file {pool_cert_path} {del_cert_args}"
        )

        # Sign the transaction with both the payment and stake keys.
        tx_signed_file = Path(self.working_dir) / (tx_name + ".signed")
        self.run_cli(
            f"{self.cli} transaction sign "
            f"--tx-body-file {tx_raw_file} --signing-key-file {payment_skey} "
            f"{signing_key_args} --signing-key-file {pool_cold_skey} "
            f"{self.network} --out-file {tx_signed_file}"
        )

        # Delete the transaction files if specified.
        if cleanup:
            self._cleanup_file(tx_draft_file)
            self._cleanup_file(tx_raw_file)

        # Submit the transaction
        if not offline:
            self.submit_transaction(tx_signed_file, cleanup)
        else:
            print(f"Signed transaction file saved to: {tx_signed_file}")

    def update_stake_pool_registration(
        self,
        pool_name,
        pool_pledge,
        pool_cost,
        pool_margin,
        pool_cold_vkey,
        pool_cold_skey,
        pool_vrf_key,
        pool_reward_vkey,
        owner_stake_vkeys,
        owner_stake_skeys,
        payment_addr,
        payment_skey,
        genesis_file,
        pool_relays=None,
        pool_metadata_url=None,
        pool_metadata_hash=None,
        folder=None,
        offline=False,
        cleanup=True,
    ):
        """Update an existing stake pool registration on the blockchain.

        Parameters
        ----------
        pool_name : str
            Pool name for file/certificate naming.
        pool_metadata : dict
            Dictionary of stake pool metadata to be converted to json.
        pool_pledge : int
            Pool pledge amount in lovelace.
        pool_cost : int
            Pool cost (fixed fee per epoch) in lovelace.
        pool_margin : float
            Pool margin (variable fee) as a percentage.
        pool_cold_vkey : str or Path
            Path to the pool's cold verification key.
        pool_cold_skey : str or Path
            Path to the pool's cold signing key.
        pool_vrf_key : str or Path
            Path to the pool's verification key.
        pool_reward_vkey : str or Path
            Path to the staking verification key that will receive pool
            rewards.
        owner_stake_vkeys : list
            List of owner stake verification keys (paths) responsible for the
            pledge.
        owner_stake_skeys : list
            List of owner stake signing keys (paths) responsible for the
            pledge.
        payment_addr : str
            Address responsible for paying the pool registration and
            transaction fees.
        payment_skey : str or Path
            Signing key for the address responsible for paying the pool
            registration and transaction fees.
        genesis_file : str or Path
            Path to the genesis file.
        pool_relays: list, optional,
            List of dictionaries each representing a pool relay. The
            dictionaries have three required keys:
                "port" specifying the relay's port number,
                "host" specifying the host name (IP, DNS, etc.),
                "host-type" specifying the type of data in the "host" key.
        pool_metadata_url : str, optional
            URL to the pool's metadata JSON file.
        pool_metadata_hash : str, optional
            Optionally specify the hash of the metadata JSON file. If this is
            not specified and the pool_metadata_hash is, then the code will
            download the file from the URL and compute the hash.
        folder : str, Path, optional
            The directory where the generated files/certs will be placed.
        offline: bool, optional
            Flag to indicate if the transactions is being generated offline.
            If true (defaults to false), the transaction file is signed but
            not sent.
        cleanup : bool, optional
            Flag that indicates if the temporary transaction files should be
            removed when finished (defaults to True).
        """

        # Get a working directory to store the generated files and make sure
        # the directory exists.
        if folder is None:
            folder = self.working_dir
        else:
            folder = Path(folder)
            if self.ssh is None:
                folder.mkdir(parents=True, exist_ok=True)
            else:
                self.run_cli(f'mkdir -p "{folder}"')

        # Get the hash of the JSON file if the URL is provided and the hash is
        # not specified.
        metadata_args = ""
        if pool_metadata_url is not None:
            if pool_metadata_hash is None:
                metadata_file = folder / "metadata_file_download.json"
                self._download_file(pool_metadata_url, metadata_file)
                result = self.run_cli(
                    f"{self.cli} stake-pool metadata-hash "
                    f"--pool-metadata-file {metadata_file}"
                )
                pool_metadata_hash = result.stdout.strip()

            # Create the arg string for the pool cert.
            metadata_args = (
                f"--metadata-url {pool_metadata_url} "
                f"--metadata-hash {pool_metadata_hash}"
            )

        # Create the relay arg string. Basically, we need a port and host arg
        # but there can be different forms of the host argument. See the
        # caradno-cli documentation. The simpliest way I could figure was to
        # use a list of dictionaries where each dict represents a relay.
        relay_args = ""
        for relay in pool_relays:
            if "ipv4" in relay["host-type"]:
                host_arg = f"--pool-relay-ipv4 {relay['host']}"
            elif "ipv6" in relay["host-type"]:
                host_arg = f"--pool-relay-ipv4 {relay['host']}"
            elif "single" in relay["host-type"]:
                host_arg = f"--single-host-pool-relay {relay['host']}"
            elif "multi" in relay["host-type"]:
                relay_args += f"--multi-host-pool-relay {relay['host']}"
                continue  # No port info for this case
            else:
                continue  # Skip if invalid host type
            port_arg = f"--pool-relay-port {relay['port']}"
            relay_args += f"{host_arg} {port_arg} "

        # Create the argument string for the list of owner verification keys.
        owner_vkey_args = ""
        for key_path in owner_stake_vkeys:
            arg = f"--pool-owner-stake-verification-key-file {key_path} "
            owner_vkey_args += arg

        # Generate Stake pool registration certificate
        pool_cert_path = folder / (pool_name + "_registration.cert")
        self.run_cli(
            f"{self.cli} stake-pool registration-certificate "
            f"--cold-verification-key-file {pool_cold_vkey} "
            f"--vrf-verification-key-file {pool_vrf_key} "
            f"--pool-pledge {pool_pledge} "
            f"--pool-cost {pool_cost} "
            f"--pool-margin {pool_margin/100} "
            f"--pool-reward-account-verification-key-file {pool_reward_vkey} "
            f"{owner_vkey_args} {relay_args} {metadata_args} "
            f"{self.network} --out-file {pool_cert_path}"
        )

        # TODO: Edit the cert free text?

        # Generate delegation certificate (pledge from each owner)
        del_cert_args = ""
        signing_key_args = ""
        for key_path in owner_stake_vkeys:
            key_path = Path(key_path)
            cert_path = key_path.parent / (key_path.stem + "_delegation.cert")
            del_cert_args += f"--certificate-file {cert_path} "
            self.run_cli(
                f"{self.cli} stake-address delegation-certificate "
                f"--stake-verification-key-file {key_path} "
                f"--cold-verification-key-file {pool_cold_vkey} "
                f"--out-file {cert_path}"
            )

        # Generate a list of owner signing key args.
        for key_path in owner_stake_skeys:
            signing_key_args += f"--signing-key-file {key_path} "

        # Get the pool deposit from the network genesis parameters.
        pool_deposit = 0  # re-registration doesn't require deposit

        # Get a list of UTXOs and sort them in decending order by value.
        utxos = self.get_utxos(payment_addr)
        utxos.sort(key=lambda k: k["Lovelace"], reverse=True)

        # Determine the TTL
        tip = self.get_tip()
        ttl = tip + self.ttl_buffer

        # Ensure the parameters file exists
        self.load_protocol_parameters()

        # Iterate through the UTXOs until we have enough funds to cover the
        # transaction. Also, create the tx_in string for the transaction.
        tx_name = datetime.now().strftime("reg_pool_%Y-%m-%d_%Hh%Mm%Ss")
        tx_draft_file = Path(self.working_dir) / (tx_name + ".draft")
        utxo_total = 0
        min_fee = 1  # make this start greater than utxo_total
        tx_in_str = ""
        for idx, utxo in enumerate(utxos):
            utxo_count = idx + 1
            utxo_total += int(utxo["Lovelace"])
            tx_in_str += f" --tx-in {utxo['TxHash']}#{utxo['TxIx']}"

            # Build a transaction draft
            self.run_cli(
                f"{self.cli} transaction build-raw{tx_in_str} "
                f"--tx-out {payment_addr}+0 --ttl 0 --fee 0 "
                f"--out-file {tx_draft_file} "
                f"--certificate-file {pool_cert_path} {del_cert_args}"
            )

            # Calculate the minimum fee
            nwit = len(owner_stake_skeys) + 2
            min_fee = self.calc_min_fee(
                tx_draft_file, utxo_count, tx_out_count=1, witness_count=nwit
            )

            if utxo_total > (min_fee + pool_deposit):
                break

        if utxo_total < min_fee:
            cost_ada = (min_fee + pool_deposit) / 1_000_000
            utxo_total_ada = utxo_total / 1_000_000
            raise ShelleyError(
                f"Transaction failed due to insufficient funds. Account "
                f"{payment_addr} cannot pay tranction costs of {cost_ada} "
                f"lovelaces because it only contains {utxo_total_ada} ADA."
            )

        # Build the transaction to submit the pool certificate and delegation
        # certificate(s) to the blockchain.
        tx_raw_file = Path(self.working_dir) / (tx_name + ".raw")
        self.run_cli(
            f"{self.cli} transaction build-raw{tx_in_str} "
            f"--tx-out {payment_addr}+{utxo_total - min_fee - pool_deposit} "
            f"--ttl {ttl} --fee {min_fee} --out-file {tx_raw_file} "
            f"--certificate-file {pool_cert_path} {del_cert_args}"
        )

        # Sign the transaction with both the payment and stake keys.
        tx_signed_file = Path(self.working_dir) / (tx_name + ".signed")
        self.run_cli(
            f"{self.cli} transaction sign "
            f"--tx-body-file {tx_raw_file} --signing-key-file {payment_skey} "
            f"{signing_key_args} --signing-key-file {pool_cold_skey} "
            f"{self.network} --out-file {tx_signed_file}"
        )

        # Delete the transaction files if specified.
        if cleanup:
            self._cleanup_file(tx_draft_file)
            self._cleanup_file(tx_raw_file)

        # Submit the transaction
        if not offline:
            self.submit_transaction(tx_signed_file, cleanup)
        else:
            print(f"Signed transaction file saved to: {tx_signed_file}")

    def retire_stake_pool(
        self,
        remaining_epochs,
        genesis_file,
        cold_vkey,
        cold_skey,
        payment_skey,
        payment_addr,
        cleanup=True,
    ):
        """Retire a stake pool using the stake pool keys.

        To retire the stake pool we need to:
        - Create a deregistration certificate and
        - Submit the certificate to the blockchain with a transaction.

        The deregistration certificate contains the epoch in which we want to
        retire the pool. This epoch must be after the current epoch and not
        later than eMax epochs in the future, where eMax is a protocol
        parameter.

        Parameters
        ----------
        remaining_epochs : int
            Epochs remaining before pool should be deregistered.
        genesis_file : str or Path
            Path to the genesis file.
        cold_vkey : str or Path
            Path to the pool's cold verification key.
        cold_skey : str or Path
            Path to the pool's cold signing key.
        payment_skey : str or Path
            Path to the payment signing key.
        payment_addr : str
            Address of the payment key.
        cleanup : bool, optional
            Flag that indicates if the temporary transaction files should be
            removed when finished (defaults to True).
        """

        # Get the network parameters
        params_file = self.load_protocol_parameters()
        e_max = self.protocol_parameters["eMax"]

        # Make sure the remaining epochs is a valid number.
        if remaining_epochs < 1:
            remaining_epochs = 1
        elif remaining_epochs > e_max:
            raise ShelleyError(
                f"Invalid number of remaining epochs ({remaining_epochs}) "
                f"prior to pool retirement. The maximum is {e_max}."
            )

        # Get the network genesis parameters
        with open(genesis_file, "r") as genfile:
            genesis_parameters = json.load(genfile)
        epoch_length = genesis_parameters["epochLength"]

        # Determine the TTL
        tip = self.get_tip()
        ttl = tip + self.ttl_buffer

        # Get the current epoch
        epoch = tip // epoch_length

        # Create deregistration certificate
        pool_dereg = self.working_dir / "pool.dereg"
        self.run_cli(
            f"{self.cli} stake-pool deregistration-certificate "
            f"--cold-verification-key-file {cold_vkey} "
            f"--epoch {epoch + remaining_epochs} --out-file {pool_dereg}"
        )

        # Get a list of UTXOs and sort them in decending order by value.
        utxos = self.get_utxos(payment_addr)
        utxos.sort(key=lambda k: k["Lovelace"], reverse=True)

        # Iterate through the UTXOs until we have enough funds to cover the
        # transaction. Also, create the tx_in string for the transaction.
        tx_draft_file = self.working_dir / "pool_dereg_tx.draft"
        utxo_total = 0
        tx_in_str = ""
        for idx, utxo in enumerate(utxos):
            utxo_count = idx + 1
            utxo_total += int(utxo["Lovelace"])
            tx_in_str += f" --tx-in {utxo['TxHash']}#{utxo['TxIx']}"

            # Build a transaction draft
            self.run_cli(
                f"{self.cli} transaction build-raw{tx_in_str} "
                f"--tx-out {payment_addr}+0 --ttl 0 --fee 0 "
                f"--out-file {tx_draft_file} --certificate-file {pool_dereg}"
            )

            # Calculate the minimum fee
            min_fee = self.calc_min_fee(
                tx_draft_file, utxo_count, tx_out_count=1, witness_count=2
            )

            if utxo_total > min_fee:
                break

        if utxo_total < min_fee:
            # cost_ada = min_fee/1_000_000
            utxo_total_ada = utxo_total / 1_000_000
            raise ShelleyError(
                f"Transaction failed due to insufficient funds. Account "
                f"{payment_addr} cannot pay tranction costs of {min_fee} "
                f"lovelaces because it only contains {utxo_total_ada} ADA."
            )

        # Build the raw transaction
        tx_raw_file = self.working_dir / "pool_dereg_tx.raw"
        self.run_cli(
            f"{self.cli} transaction build-raw{tx_in_str} "
            f"--tx-out {payment_addr}+{utxo_total - min_fee} --ttl {ttl} "
            f"--fee {min_fee} --out-file {tx_raw_file} "
            f"--certificate-file {pool_dereg}"
        )

        # Sign it with both the payment signing key and the cold signing key.
        tx_signed_file = self.working_dir / "pool_dereg_tx.signed"
        self.run_cli(
            f"{self.cli} transaction sign "
            f"--tx-body-file {tx_raw_file} "
            f"--signing-key-file {payment_skey} "
            f"--signing-key-file {cold_skey} "
            f"{self.network} --out-file {tx_signed_file}"
        )

        # Submit the transaction
        self.run_cli(
            f"{self.cli} transaction submit "
            f"--tx-file {tx_signed_file} {self.network}"
        )

        # Delete the transaction files if specified.
        if cleanup:
            self._cleanup_file(tx_draft_file)
            self._cleanup_file(tx_raw_file)
            self._cleanup_file(tx_signed_file)

    def get_stake_pool_id(self, cold_vkey) -> str:
        """Return the stake pool ID associated with the supplied cold key.

        Parameters
        ----------
        cold_vkey : str or Path
            Path to the pool's cold verification key.

        Returns
        ----------
        str
            The stake pool id.
        """
        result = self.run_cli(
            f"{self.cli} stake-pool id " f"--verification-key-file {cold_vkey}"
        )
        pool_id = result.stdout
        return pool_id

    def claim_staking_rewards(
        self,
        stake_addr,
        stake_skey,
        receive_addr,
        payment_skey,
        payment_addr=None,
        offline=False,
        cleanup=True,
    ):
        """Withdraw staking address rewards to a spending address.

        Thanks to @ATADA_Stakepool who's scripts greatly influenced the 
        development of this function. https://github.com/gitmachtl/scripts

        Parameters
        ----------
        stake_addr : str
            Staking address from which to withdraw the rewards.
        stake_skey : str or Path
            Path to the staking address signing key.
        receive_addr : str
            Spending address to receive the rewards. 
        payment_skey : str or Path
            Path to the signing key for the account paying the tx fees.
        payment_addr : str, optional
            Optionally use a second account to pay the tx fees.
        offline: bool, optional
            Flag to indicate if the transactions is being generated offline.
            If true (defaults to false), the transaction file is signed but
            not sent.
        cleanup : bool, optional
            Flag that indicates if the temporary transaction files should be
            removed when finished (defaults to True).
        """

        # Calculate the amount to withdraw.
        rewards = self.get_rewards_balance(stake_addr)
        if rewards <= 0.0:
            raise ShelleyError(
                f"No rewards availible in stake address {stake_addr}."
            )
        withdrawal_str = f"{stake_addr}+{rewards}"

        # Get a list of UTXOs and sort them in decending order by value.
        if payment_addr is None:
            payment_addr = receive_addr
        utxos = self.get_utxos(payment_addr)
        if len(utxos) < 1:
            raise ShelleyError(
                f"Transaction failed due to insufficient funds. "
                f"Account {payment_addr} cannot pay tranction costs because "
                "it does not contain any ADA."
            )
        utxos.sort(key=lambda k: k["Lovelace"], reverse=True)

        # Build a transaction name
        tx_name = datetime.now().strftime("claim_rewards_%Y-%m-%d_%Hh%Mm%Ss")

        # Ensure the parameters file exists
        self.load_protocol_parameters()

        # Determine the TTL
        tip = self.get_tip()
        ttl = tip + self.ttl_buffer

        # Iterate through the UTXOs until we have enough funds to cover the
        # transaction. Also, create the tx_in string for the transaction.
        tx_draft_file = Path(self.working_dir) / (tx_name + ".draft")
        utxo_total = 0
        tx_in_str = ""
        for idx, utxo in enumerate(utxos):
            utxo_count = idx + 1
            utxo_total += int(utxo["Lovelace"])
            tx_in_str += f" --tx-in {utxo['TxHash']}#{utxo['TxIx']}"

            # If the address receiving the funds is also paying the TX fee.
            if payment_addr == receive_addr:
                # Build a transaction draft
                self.run_cli(
                    f"{self.cli} transaction build-raw{tx_in_str} "
                    f"--tx-out {receive_addr}+0 --ttl 0 --fee 0 "
                    f"--withdrawal {withdrawal_str} --out-file {tx_draft_file}"
                )

                # Calculate the minimum fee
                min_fee = self.calc_min_fee(
                    tx_draft_file, utxo_count, tx_out_count=1, witness_count=2
                )

            # If another address is paying the TX fee.
            else:
                # Build a transaction draft
                self.run_cli(
                    f"{self.cli} transaction build-raw{tx_in_str} "
                    f"--tx-out {receive_addr}+0 --tx-out {payment_addr}+0 "
                    f"--ttl 0 --fee 0 --withdrawal {withdrawal_str} "
                    f"--out-file {tx_draft_file}"
                )

                # Calculate the minimum fee
                min_fee = self.calc_min_fee(
                    tx_draft_file, utxo_count, tx_out_count=2, witness_count=2
                )

            # If we have enough in the UTXO we are done, otherwise, continue.
            if utxo_total > min_fee:
                break

        if utxo_total < min_fee:
            cost_ada = min_fee / 1_000_000
            utxo_total_ada = utxo_total / 1_000_000
            a = receive_addr if payment_addr == receive_addr else payment_addr
            raise ShelleyError(
                f"Transaction failed due to insufficient funds. "
                f"Account {a} cannot pay tranction costs of {cost_ada} "
                f"ADA because it only contains {utxo_total_ada} ADA."
            )

        # Build the transaction.
        tx_raw_file = Path(self.working_dir) / (tx_name + ".raw")
        if payment_addr == receive_addr:
            # If the address receiving the funds is also paying the TX fee.
            self.run_cli(
                f"{self.cli} transaction build-raw{tx_in_str} "
                f"--tx-out {receive_addr}+{utxo_total - min_fee + rewards} "
                f"--ttl {ttl} --fee {min_fee} --withdrawal {withdrawal_str} "
                f"--out-file {tx_raw_file}"
            )
        else:
            # If another address is paying the TX fee.
            self.run_cli(
                f"{self.cli} transaction build-raw{tx_in_str} "
                f"--tx-out {payment_addr}+{utxo_total - min_fee} "
                f"--tx-out {receive_addr}+{rewards} "
                f"--ttl {ttl} --fee {min_fee} --withdrawal {withdrawal_str} "
                f"--out-file {tx_raw_file}"
            )

        # Sign the transaction with both the payment and stake keys.
        tx_signed_file = Path(self.working_dir) / (tx_name + ".signed")
        self.run_cli(
            f"{self.cli} transaction sign "
            f"--tx-body-file {tx_raw_file} --signing-key-file {payment_skey} "
            f"--signing-key-file {stake_skey} {self.network} "
            f"--out-file {tx_signed_file}"
        )

        # Delete the intermediate transaction files if specified.
        if cleanup:
            self._cleanup_file(tx_draft_file)
            self._cleanup_file(tx_raw_file)

        # Submit the transaction
        if not offline:
            self.submit_transaction(tx_signed_file, cleanup)
        else:
            print(f"Signed transaction file saved to: {tx_signed_file}")

    def convert_itn_keys(self, itn_prv_key, itn_pub_key, folder=None) -> str:
        """Convert ITN account keys to Shelley staking keys.

        Parameters
        ----------
        itn_prv_key : str or Path
            Path to the ITN private key file.
        itn_pub_key : str or Path
            Path to the ITN public key file.
        folder : str or Path, optional
            The directory where the generated files/certs will be placed.

        Returns
        -------
        str
            New Shelley staking wallet address.

        Raises
        ------
        ShelleyError
            If the private key is not in a known format.
        """

        # Get a working directory to store the generated files and make sure
        # the directory exists.
        if folder is None:
            folder = self.working_dir
        else:
            folder = Path(folder)
            if self.ssh is None:
                folder.mkdir(parents=True, exist_ok=True)
            else:
                self.run_cli(f'mkdir -p "{folder}"')

        # Open the private key file to check its format.
        prvkey = open(itn_prv_key, "r").read()

        # Convert the private key
        skey_file = folder / (Path(itn_prv_key).stem + "_shelley_staking.skey")
        if prvkey[:8] == "ed25519e":
            self.run_cli(
                f"{self.cli} key convert-itn-extended-key "
                f"--itn-signing-key-file {itn_prv_key} "
                f"--out-file {skey_file}"
            )
        elif prvkey[:8] == "ed25519b":
            self.run_cli(
                f"{self.cli} key convert-itn-bip32-key "
                f"--itn-signing-key-file {itn_prv_key} "
                f"--out-file {skey_file}"
            )
        elif prvkey[:7] == "ed25519":
            self.run_cli(
                f"{self.cli} key convert-itn-key "
                f"--itn-signing-key-file {itn_prv_key} "
                f"--out-file {skey_file}"
            )
        else:
            raise ShelleyError("Invalid ITN private key format.")

        # Convert the public key
        vkey_file = folder / (Path(itn_pub_key).stem + "_shelley_staking.vkey")
        self.run_cli(
            f"{self.cli} key convert-itn-key "
            f"--itn-verification-key-file {itn_pub_key} "
            f"--out-file {vkey_file}"
        )

        # Create the staking address
        addr_file = folder / (Path(itn_pub_key).stem + "_shelley_staking.addr")
        self.run_cli(
            f"{self.cli} stake-address build "
            f"--stake-verification-key-file {vkey_file} "
            f"--out-file {addr_file} {self.network}"
        )

        # Read the file and return the staking address.
        addr = self._load_text_file(addr_file).strip()
        return addr

    def get_rewards_balance(self, stake_addr) -> int:
        """Return the balance in a Shelley staking rewards account.

        Parameters
        ----------
        addr : str
            Staking address.

        Returns
        ----------
        int
            Rewards balance in lovelaces. 
        """
        result = self.run_cli(
            f"{self.cli} query stake-address-info --address "
            f"{stake_addr} {self.network}"
        )
        if "Failed" in result.stdout:
            raise ShelleyError(result.stdout)
        if len(result.stderr) > 0:
            raise ShelleyError(result.stderr)
        info = json.loads(result.stdout)
        balance = sum(b["rewardAccountBalance"] for b in info)
        return balance

    def empty_account(
        self, to_addr, from_addr, key_file, offline=False, cleanup=True
    ):
        """Send all ADA contained in one address to another address.

        Parameters
        ----------
        to_addr : str
            Address to send the ADA to.
        from_addr : str
            Address to send the ADA from.
        key_file : str or Path
            Path to the send address signing key file.
        offline: bool, optional
            Flag to indicate if the transactions is being generated offline.
            If true (defaults to false), the transaction file is signed but
            not sent.
        cleanup : bool, optional
            Flag that indicates if the temporary transaction files should be
            removed when finished (defaults to True).
        """

        # Get the address balance
        bal = self.query_balance(from_addr)

        # Build a transaction name
        tx_name = datetime.now().strftime("empty_acct_%Y-%m-%d_%Hh%Mm%Ss")

        # Get a list of UTxOs and create the tx_in string.
        tx_in_str = ""
        utxos = self.get_utxos(from_addr)
        for utxo in utxos:
            tx_in_str += f" --tx-in {utxo['TxHash']}#{utxo['TxIx']}"

        # Build a transaction draft
        tx_draft_file = Path(self.working_dir) / (tx_name + ".draft")
        self.run_cli(
            f"{self.cli} transaction build-raw{tx_in_str} "
            f"--tx-out {to_addr}+0 "
            f"--ttl 0 --fee 0 --out-file {tx_draft_file}"
        )

        # Calculate the minimum fee
        min_fee = self.calc_min_fee(
            tx_draft_file, len(utxos), tx_out_count=2, witness_count=1
        )

        if min_fee > bal:
            raise ShelleyError(
                f"Transaction failed due to insufficient funds. "
                f"Account {from_addr} cannot send {bal/1_000_000} ADA plus "
                f"fees of {min_fee/1_000_000} ADA to account {to_addr} "
                f"because it only contains {bal/1_000_000.} ADA."
            )
            # Maybe this should fail more gracefully, but higher level logic
            # can also just catch the error and handle it.

        # Determine the slot where the transaction will become invalid. Get the
        # current slot number and add a buffer to it.
        tip = self.get_tip()
        ttl = tip + self.ttl_buffer

        # Build the transaction
        tx_raw_file = Path(self.working_dir) / (tx_name + ".raw")
        self.run_cli(
            f"{self.cli} transaction build-raw{tx_in_str} "
            f"--tx-out {to_addr}+{(bal - min_fee):.0f} "
            f"--ttl {ttl} --fee {min_fee} --out-file {tx_raw_file}"
        )

        # Sign the transaction with the signing key
        tx_signed_file = self.sign_transaction(tx_raw_file, [key_file])

        # Delete the intermediate transaction files if specified.
        if cleanup:
            self._cleanup_file(tx_raw_file)

        # Submit the transaction
        if not offline:
            self.submit_transaction(tx_signed_file, cleanup)
        else:
            print(f"Signed transaction file saved to: {tx_signed_file}")


if __name__ == "__main__":
    # Not used as a script
    pass
