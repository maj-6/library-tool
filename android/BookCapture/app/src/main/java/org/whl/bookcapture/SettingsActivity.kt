package org.whl.bookcapture

import android.os.Bundle
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.whl.bookcapture.databinding.ActivitySettingsBinding

/** Supabase project URL/key + a device label (shows up in imported entries). */
class SettingsActivity : AppCompatActivity() {

    private lateinit var binding: ActivitySettingsBinding

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivitySettingsBinding.inflate(layoutInflater)
        setContentView(binding.root)

        binding.url.setText(Prefs.supabaseUrl(this))
        binding.key.setText(Prefs.supabaseKey(this))
        binding.device.setText(Prefs.deviceName(this))

        binding.save.setOnClickListener {
            Prefs.save(this,
                binding.url.text.toString(),
                binding.key.text.toString(),
                binding.device.text.toString())
            binding.msg.text = getString(R.string.saved)
        }

        binding.test.setOnClickListener {
            Prefs.save(this,
                binding.url.text.toString(),
                binding.key.text.toString(),
                binding.device.text.toString())
            binding.msg.text = getString(R.string.testing)
            lifecycleScope.launch {
                val err = withContext(Dispatchers.IO) {
                    if (!Prefs.configured(this@SettingsActivity)) "URL / key missing"
                    else SupabaseClient(
                        Prefs.supabaseUrl(this@SettingsActivity),
                        Prefs.supabaseKey(this@SettingsActivity)).testConnection()
                }
                binding.msg.text = err ?: getString(R.string.connection_ok)
            }
        }
    }
}
