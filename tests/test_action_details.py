"""The board shows what the agent runs; only recognized credentials are masked."""
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from session_board import action_details as dettagli  # noqa: E402

COMANDO = ('cat conoscenza/handoff-2026-11-19.md; echo ---; tail -5 conoscenza/registro-notti.md; '
           'echo ---; export XDG_RUNTIME_DIR=/run/user/$(id -u); '
           'systemctl --user list-timers pulizia-contatti.timer --no-pager')


class ProvaDettagli(unittest.TestCase):
    def dettaglio(self, comando):
        return dettagli.describe_details('Bash', {'command': comando})['detail']

    def test_il_comando_composto_si_vede_intero(self):
        self.assertEqual(self.dettaglio(COMANDO), COMANDO)

    def test_lo_script_su_piu_righe_si_vede_intero(self):
        script = "python3 - <<'FINE'\nimport csv\nprint(sum(1 for _ in open('contatti.csv')))\nFINE"
        self.assertEqual(self.dettaglio(script), script)

    def test_pipe_e_sostituzioni_non_vengono_tagliate(self):
        comando = "grep -c . contatti.csv | awk '{print $1}' && echo \"oggi $(date +%F)\""
        self.assertEqual(self.dettaglio(comando), comando)

    def test_i_token_riconosciuti_restano_nascosti(self):
        casi = ('ghp_' + 'A' * 36, 'github_pat_' + 'B' * 30, 'sk-' + 'c' * 24, '123456789:' + 'D' * 35)
        for segreto in casi:
            visto = self.dettaglio(f'curl -s https://esempio.it/api -H "X-Prova: {segreto}"')
            self.assertNotIn(segreto, visto)
            self.assertIn('[omesso]', visto)
            self.assertIn('curl -s https://esempio.it/api', visto)

    def test_i_valori_con_nome_da_segreto_restano_nascosti(self):
        visto = self.dettaglio('GITHUB_TOKEN=abc123 gh repo list; export PASSWORD="due parole"; fai --api-key=xyz')
        for segreto in ('abc123', 'due parole', 'xyz'):
            self.assertNotIn(segreto, visto)
        self.assertIn('gh repo list', visto)

    def test_una_variabile_qualsiasi_si_vede(self):
        self.assertEqual(self.dettaglio('ORA=02:00 ./pulisci.sh'), 'ORA=02:00 ./pulisci.sh')

    def test_intestazioni_e_password_negli_indirizzi_restano_nascoste(self):
        visto = self.dettaglio("curl -H 'Authorization: Bearer abcdefgh' https://utente:parola@esempio.it/x?pagina=2")
        self.assertNotIn('abcdefgh', visto)
        self.assertNotIn('parola', visto)
        self.assertIn('https://utente:[omesso]@esempio.it/x?pagina=2', visto)

    def test_la_chiave_privata_resta_nascosta(self):
        visto = self.dettaglio('echo "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----" > k')
        self.assertNotIn('AAAA', visto)
        self.assertIn('[chiave privata omessa]', visto)

    def test_il_percorso_del_file_si_vede(self):
        for percorso in ('/home/agente/segreti/github.txt', '/home/agente/progetti/pulizia-contatti/.env'):
            visto = dettagli.describe_details('Read', {'file_path': percorso})['detail']
            self.assertEqual(visto, percorso)

    def test_lo_script_lunghissimo_viene_abbreviato_e_lo_dice(self):
        esito = dettagli.describe_details('Bash', {'command': 'echo ' + 'x' * 30000})
        self.assertTrue(esito['detail_truncated'])
        self.assertLessEqual(len(esito['detail']), dettagli.MAX_DETAIL)

    def test_l_approvazione_mostra_il_comando_che_si_approva(self):
        comando = 'git commit -m "$(cat <<\'FINE\'\nSalva il registro\nFINE\n)"'
        # questions._command_detail passa da qui: non deve più sostituire il
        # messaggio del commit con un segnaposto.
        self.assertEqual(dettagli.sanitize_detail(comando)['detail'], comando)
        sorgente = (Path(__file__).resolve().parents[1] / 'session_board' / 'questions.py').read_text()
        self.assertNotIn('script omesso', sorgente)


if __name__ == '__main__':
    unittest.main()
