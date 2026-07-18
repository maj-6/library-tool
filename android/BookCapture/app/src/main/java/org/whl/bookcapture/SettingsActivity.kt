package org.whl.bookcapture

import android.content.Intent
import android.os.Build
import android.os.Bundle
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.whl.bookcapture.databinding.ActivitySettingsBinding

/**
 * Account, device label and API keys. The keys belong to the ACCOUNT, not the
 * device: saving pushes them to the cloud profile, so the desktop (and the
 * next phone) picks them up without pasting anything twice.
 */
class SettingsActivity : AppCompatActivity() {

    private lateinit var binding: ActivitySettingsBinding

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivitySettingsBinding.inflate(layoutInflater)
        setContentView(binding.root)

        binding.accountEmail.text = Prefs.email(this)
        binding.displayName.setText(Prefs.displayName(this))
        binding.device.setText(Prefs.deviceName(this))
        binding.mistralKey.setText(Prefs.mistralKey(this))
        binding.deepseekKey.setText(Prefs.deepseekKey(this))

        // viewfinder sharpen is a GPU shader on the preview — Android 13+ only
        binding.sharpenPreview.isEnabled = Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU
        binding.sharpenPreview.isChecked = Prefs.sharpenPreview(this)
        binding.sharpenPreview.setOnCheckedChangeListener { _, on ->
            Prefs.setSharpenPreview(this, on)
        }

        // Voice control is opt-in: enabling it here is what makes the capture
        // screen ask for the mic and download the model on its next resume.
        binding.voiceControl.isChecked = Prefs.voiceControl(this)
        binding.voiceControl.setOnCheckedChangeListener { _, on ->
            Prefs.setVoiceControl(this, on)
        }

        // transport (Cloud / LAN / Auto) + LAN pairing
        when (Prefs.transport(this)) {
            "lan" -> binding.transportLan.isChecked = true
            "auto" -> binding.transportAuto.isChecked = true
            else -> binding.transportCloud.isChecked = true
        }
        binding.lanHost.setText(Prefs.lanHost(this))
        binding.lanToken.setText(Prefs.lanToken(this))
        binding.lanTest.setOnClickListener {
            Prefs.setLan(this, binding.lanHost.text.toString(), binding.lanToken.text.toString())
            binding.msg.text = getString(R.string.testing)
            lifecycleScope.launch {
                val ok = withContext(Dispatchers.IO) {
                    try { LanClient(this@SettingsActivity).ping() } catch (_: Exception) { false }
                }
                binding.msg.text = getString(if (ok) R.string.lan_ok else R.string.lan_unreachable)
            }
        }

        // freshen the cache; another device may have changed the keys. Only
        // overwrite a field the user has NOT touched since it was populated,
        // so a slow network pull can't wipe a key they are mid-typing.
        val shown = mapOf(
            binding.displayName to Prefs.displayName(this),
            binding.mistralKey to Prefs.mistralKey(this),
            binding.deepseekKey to Prefs.deepseekKey(this))
        lifecycleScope.launch(Dispatchers.IO) {
            try {
                Auth.pullProfile(this@SettingsActivity)
                withContext(Dispatchers.Main) {
                    val fresh = mapOf(
                        binding.displayName to Prefs.displayName(this@SettingsActivity),
                        binding.mistralKey to Prefs.mistralKey(this@SettingsActivity),
                        binding.deepseekKey to Prefs.deepseekKey(this@SettingsActivity))
                    for ((field, was) in shown)
                        if (field.text.toString() == was) field.setText(fresh[field])
                }
            } catch (_: Exception) { /* offline: the cache stands */ }
        }

        binding.save.setOnClickListener {
            Prefs.setDeviceName(this, binding.device.text.toString())
            Prefs.setTransport(this, when {
                binding.transportLan.isChecked -> "lan"
                binding.transportAuto.isChecked -> "auto"
                else -> "cloud"
            })
            Prefs.setLan(this, binding.lanHost.text.toString(), binding.lanToken.text.toString())
            UploadWorker.enqueue(this)      // a transport change should drain now
            binding.msg.text = getString(R.string.testing)
            lifecycleScope.launch {
                // pushing the profile needs a cloud sign-in; LAN-only users may
                // not have one, so skip it rather than surfacing "signed out"
                val err = if (Auth.signedIn(this@SettingsActivity))
                    withContext(Dispatchers.IO) {
                        Auth.pushProfile(this@SettingsActivity,
                            binding.displayName.text.toString(),
                            binding.mistralKey.text.toString(),
                            binding.deepseekKey.text.toString())
                    } else null
                binding.msg.text = err ?: getString(R.string.saved)
                if (err == null) ProcessWorker.enqueue(this@SettingsActivity)
            }
        }

        binding.test.setOnClickListener {
            binding.msg.text = getString(R.string.testing)
            lifecycleScope.launch {
                val err = withContext(Dispatchers.IO) {
                    SupabaseClient(this@SettingsActivity).testConnection()
                }
                binding.msg.text = err ?: getString(R.string.connection_ok)
                if (err == null) UploadWorker.enqueue(this@SettingsActivity)
            }
        }

        binding.signOut.setOnClickListener {
            Auth.signOut(this)
            startActivity(Intent(this, LoginActivity::class.java))
            finish()
        }
    }
}
