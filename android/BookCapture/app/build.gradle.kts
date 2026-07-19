plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// Release signing comes from the environment (local shell or CI secrets), so no
// key material ever sits in the repo. Without WHL_KEYSTORE_FILE the release
// build falls back to the debug key: still installable for sideloading, but
// Android treats builds signed with different keys as different authors, so an
// update over an old install needs an uninstall first. Keep one keystore.
val releaseKeystore: String? =
    System.getenv("WHL_KEYSTORE_FILE")?.takeIf { it.isNotBlank() }

// The Supabase project the app signs into. The anon key is public by design
// (the website ships it to every visitor); it is the LOGIN that authorizes
// anything. CI injects these from the repo variables; a blank fallback just
// means Settings must point at a project before first use. Escaped so a stray
// quote/backslash in a var can't break the generated BuildConfig string.
fun env(name: String) = (System.getenv(name)?.trim() ?: "")
    .replace("\\", "\\\\").replace("\"", "\\\"")

android {
    namespace = "org.whl.bookcapture"
    // CameraX 1.5.x publishes API-35 AAR metadata. compileSdk changes build
    // visibility only; targetSdk stays at 34 until runtime changes are audited.
    compileSdk = 35

    defaultConfig {
        applicationId = "org.whl.bookcapture"
        minSdk = 26
        targetSdk = 34
        versionCode = 24
        versionName = "0.5.1-alpha.5"
        buildConfigField("String", "SUPABASE_URL", "\"${env("WHL_SUPABASE_URL")}\"")
        buildConfigField("String", "SUPABASE_ANON_KEY", "\"${env("WHL_SUPABASE_ANON_KEY")}\"")
    }

    signingConfigs {
        if (releaseKeystore != null) {
            create("release") {
                storeFile = file(releaseKeystore)
                storePassword = System.getenv("WHL_KEYSTORE_PASSWORD")
                keyAlias = System.getenv("WHL_KEY_ALIAS") ?: "bookcapture"
                keyPassword = System.getenv("WHL_KEY_PASSWORD")
                    ?: System.getenv("WHL_KEYSTORE_PASSWORD")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = if (releaseKeystore != null)
                signingConfigs.getByName("release")
            else
                signingConfigs.getByName("debug")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    buildFeatures {
        viewBinding = true
        buildConfig = true
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.4")
    implementation("androidx.exifinterface:exifinterface:1.3.7")

    // 1.6.x requires compileSdk 36 + AGP 8.9.1, but API 36 is not installed in
    // the supported local toolchain. 1.5.3 is the newest compatible stable.
    val camerax = "1.5.3"
    implementation("androidx.camera:camera-core:$camerax")
    implementation("androidx.camera:camera-camera2:$camerax")
    implementation("androidx.camera:camera-lifecycle:$camerax")
    implementation("androidx.camera:camera-view:$camerax")

    implementation("androidx.work:work-runtime-ktx:2.9.1")

    testImplementation("junit:junit:4.13.2")

    // android.jar's org.json is a stub that throws "not mocked" off-device, so
    // JVM tests over manifest/sidecar JSON need a real implementation on the
    // test classpath. Test-only: the app keeps using the platform's.
    testImplementation("org.json:json:20240303")

    // offline keyword spotting ("start" / "photo" / "done" / "cancel")
    implementation("com.alphacephei:vosk-android:0.3.47")
}
