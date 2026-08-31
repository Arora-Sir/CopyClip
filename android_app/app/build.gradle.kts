plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

android {
    namespace = "com.mohitarora.copyclip"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.mohitarora.copyclip"
        minSdk = 26
        targetSdk = 36
        // 0.x: this is the Shizuku-AIDL spike milestone (see bugs/SPIKE-ANDROID-USERSERVICE.md),
        // not yet the full ClipBridge-logic port described in ARCHITECTURE.md.
        versionCode = 1
        versionName = "0.1.0-spike"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
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
        compose = true
        aidl = true
        buildConfig = true
    }
}

dependencies {
    implementation(platform("androidx.compose:compose-bom:2025.01.00"))
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.material:material-icons-extended")
    implementation("androidx.activity:activity-compose:1.10.0")
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.7")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")

    // MIT licensed (github.com/RikkaApps/Shizuku-API). Not a hidden-API jar: this is the
    // official client library for talking to Shizuku's own privileged server over Binder --
    // see ARCHITECTURE.md's "Android app" section for why this is the one dependency this
    // project's usual zero-third-party-dependency rule makes room for.
    implementation("dev.rikka.shizuku:api:13.1.5")
    implementation("dev.rikka.shizuku:provider:13.1.5")
}
